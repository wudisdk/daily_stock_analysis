# -*- coding: utf-8 -*-
"""Tests for AI candidate snapshot turnover artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.services.ai_snapshot_turnover import (
    audit_turnover,
    build_turnover_rows,
    main as turnover_main,
    write_turnover_outputs,
)


def _snapshot_row(code: str, rank: int, trade_date: str = "2026-05-28") -> dict:
    return {
        "schema_version": "1.1",
        "snapshot_kind": "post_analysis_candidate",
        "candidate_source": "daily_analysis",
        "candidate_rank": rank,
        "trade_date": trade_date,
        "stock_code": code,
        "stock_name": code,
        "model_used": "deepseek/deepseek-v4-pro",
        "input_snapshot_hash": f"{rank:064d}"[-64:],
    }


def _write_jsonl(path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_build_turnover_rows_compares_ranked_snapshot_buckets() -> None:
    base_rows = [_snapshot_row(code, idx, "2026-05-27") for idx, code in enumerate(["A", "B", "C", "D", "E"], 1)]
    compare_rows = [_snapshot_row(code, idx, "2026-05-28") for idx, code in enumerate(["A", "C", "E", "F", "G"], 1)]

    rows = build_turnover_rows(
        base_rows,
        compare_rows,
        generated_at=datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc),
    )

    top5 = next(row for row in rows if row["bucket"] == "top_5")
    assert top5["base_trade_date"] == "2026-05-27"
    assert top5["compare_trade_date"] == "2026-05-28"
    assert top5["retained_count"] == 3
    assert top5["added_count"] == 2
    assert top5["removed_count"] == 2
    assert top5["retention_rate"] == 0.6
    assert top5["candidate_turnover_rate"] == 0.571429
    assert top5["jaccard_overlap_rate"] == 0.428571
    assert top5["retained_stock_codes"] == ["A", "C", "E"]
    assert top5["added_stock_codes"] == ["F", "G"]
    assert top5["removed_stock_codes"] == ["B", "D"]


def test_write_turnover_outputs_writes_summary_and_audit_files(tmp_path) -> None:
    base_path = tmp_path / "base.jsonl"
    compare_path = tmp_path / "compare.jsonl"
    _write_jsonl(base_path, [_snapshot_row("A", 1, "2026-05-27"), _snapshot_row("B", 2, "2026-05-27")])
    _write_jsonl(compare_path, [_snapshot_row("A", 1, "2026-05-28"), _snapshot_row("C", 2, "2026-05-28")])

    paths = write_turnover_outputs(
        base_path,
        compare_path,
        output_dir=tmp_path,
        generated_at=datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc),
    )

    assert {path.name for path in paths} == {
        "stock_ai_candidate_turnover_latest.json",
        "stock_ai_candidate_turnover_20260528.json",
        "stock_ai_candidate_turnover_latest.csv",
        "stock_ai_candidate_turnover_20260528.csv",
        "stock_ai_candidate_turnover_audit_latest.json",
        "stock_ai_candidate_turnover_audit_20260528.json",
        "stock_ai_candidate_turnover_audit_latest.csv",
        "stock_ai_candidate_turnover_audit_20260528.csv",
    }
    rows = json.loads((tmp_path / "stock_ai_candidate_turnover_latest.json").read_text(encoding="utf-8"))
    assert rows[0]["bucket"] == "all"
    audit = json.loads((tmp_path / "stock_ai_candidate_turnover_audit_latest.json").read_text(encoding="utf-8"))
    assert audit["overall_status"] == "PASS"


def test_audit_turnover_fails_sensitive_payload() -> None:
    base_rows = [_snapshot_row("A", 1, "2026-05-27")]
    compare_rows = [_snapshot_row("A", 1, "2026-05-28")]
    compare_rows[0]["api_key"] = "sk-redacted-placeholder-123456"
    rows = build_turnover_rows(base_rows, compare_rows)

    audit = audit_turnover(
        base_rows,
        compare_rows,
        rows,
        base_snapshot_path="base.jsonl",
        compare_snapshot_path="compare.jsonl",
        output_dir=".",
    )
    checks = {check["check_id"]: check for check in audit["checks"]}

    assert audit["overall_status"] == "FAIL"
    assert checks["turnover_low_sensitivity_boundary"]["status"] == "FAIL"


def test_turnover_cli_fails_on_audit_fail_unless_no_fail(tmp_path) -> None:
    base_path = tmp_path / "base.jsonl"
    compare_path = tmp_path / "compare.jsonl"
    _write_jsonl(base_path, [_snapshot_row("A", 1, "2026-05-27")])
    _write_jsonl(compare_path, [{"schema_version": "1.1", "candidate_rank": 1, "trade_date": "2026-05-28"}])

    args = [
        "--base-snapshot-path",
        str(base_path),
        "--compare-snapshot-path",
        str(compare_path),
        "--output-dir",
        str(tmp_path),
    ]

    assert turnover_main(args) == 1
    assert turnover_main([*args, "--no-fail"]) == 0
