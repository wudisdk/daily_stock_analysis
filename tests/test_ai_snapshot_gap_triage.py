# -*- coding: utf-8 -*-
"""Tests for AI snapshot data gap triage artifacts."""

from __future__ import annotations

import json

from src.services.ai_snapshot_gap_triage import (
    audit_gap_triage,
    build_gap_triage_rows,
    build_gap_triage_summary_rows,
    main as gap_triage_main,
    render_gap_triage_markdown,
    write_gap_triage_outputs,
)


def _audit() -> dict:
    return {
        "generated_at": "2026-05-29T00:00:00+00:00",
        "latest_trade_date": "2026-05-28",
        "checks": [
            {
                "check_id": "data_coverage_block_status",
                "status": "WARN",
                "message": "critical data coverage blocks are missing or weak",
                "evidence": {
                    "weak_block_rows": [
                        {
                            "row": {
                                "stock_code": "688981",
                                "candidate_rank": 1,
                                "trade_date": "2026-05-28",
                            },
                            "block": "quote",
                            "status": "missing",
                            "missing_reasons": ["realtime_quote_missing"],
                        },
                        {
                            "row": {
                                "stock_code": "600519",
                                "candidate_rank": 2,
                                "trade_date": "2026-05-28",
                            },
                            "block": "quote",
                            "status": "missing",
                            "missing_reasons": ["realtime_quote_missing"],
                        },
                        {
                            "row": {
                                "stock_code": "000001",
                                "candidate_rank": 3,
                                "trade_date": "2026-05-28",
                            },
                            "block": "news",
                            "status": "missing",
                            "missing_reasons": ["news_context_missing"],
                        },
                    ]
                },
            }
        ],
    }


def test_build_gap_triage_rows_aggregates_by_block_and_reason() -> None:
    rows = build_gap_triage_rows(_audit())

    assert [row["gap_id"] for row in rows] == [
        "quote:realtime_quote_missing",
        "news:news_context_missing",
    ]
    quote = rows[0]
    assert quote["severity"] == "WARN"
    assert quote["critical_block"] is True
    assert quote["affected_row_count"] == 2
    assert quote["affected_stock_codes"] == ["688981", "600519"]
    assert quote["affected_candidate_ranks"] == ["1", "2"]
    assert quote["action_bucket"] == "realtime_quote_source_gap"
    news = rows[1]
    assert news["severity"] == "INFO"
    assert news["action_bucket"] == "news_context_gap"


def test_build_gap_triage_summary_rows_groups_action_buckets() -> None:
    rows = build_gap_triage_rows(_audit())

    summary = build_gap_triage_summary_rows(rows)

    assert [row["action_bucket"] for row in summary] == [
        "realtime_quote_source_gap",
        "news_context_gap",
    ]
    quote = summary[0]
    assert quote["severity"] == "WARN"
    assert quote["action_state"] == "review_or_fix_before_trusting_score"
    assert quote["gap_count"] == 1
    assert quote["affected_row_count"] == 2
    assert quote["critical_gap_count"] == 1
    assert quote["blocks"] == ["quote"]
    assert quote["missing_reasons"] == ["realtime_quote_missing"]
    news = summary[1]
    assert news["severity"] == "INFO"
    assert news["action_state"] == "track_optional_source_debt"
    assert news["critical_gap_count"] == 0


def test_audit_gap_triage_warns_on_visible_critical_gaps_and_checks_summary() -> None:
    rows = build_gap_triage_rows(_audit())
    summary = build_gap_triage_summary_rows(rows)

    audit = audit_gap_triage(rows, summary)

    assert audit["overall_status"] == "WARN"
    checks = {check["check_id"]: check for check in audit["checks"]}
    assert checks["gap_triage_detail_schema"]["status"] == "PASS"
    assert checks["gap_triage_summary_schema"]["status"] == "PASS"
    assert checks["gap_triage_summary_consistency"]["status"] == "PASS"
    assert checks["gap_triage_critical_gap_visibility"]["status"] == "WARN"
    assert checks["gap_triage_critical_gap_visibility"]["evidence"]["critical_gap_count"] == 1
    assert checks["gap_triage_optional_gap_visibility"]["evidence"]["optional_gap_count"] == 1
    assert checks["gap_triage_low_sensitivity_boundary"]["status"] == "PASS"


def test_audit_gap_triage_fails_summary_mismatch_and_sensitive_payload() -> None:
    rows = build_gap_triage_rows(_audit())
    rows[0]["api_key"] = "sk-secret-like-value-abcdef"
    summary = build_gap_triage_summary_rows(rows)
    summary[0]["affected_row_count"] = 999

    audit = audit_gap_triage(rows, summary)

    assert audit["overall_status"] == "FAIL"
    checks = {check["check_id"]: check for check in audit["checks"]}
    assert checks["gap_triage_summary_consistency"]["status"] == "FAIL"
    assert checks["gap_triage_low_sensitivity_boundary"]["status"] == "FAIL"


def test_write_gap_triage_outputs_writes_latest_dated_csv_and_markdown(tmp_path) -> None:
    audit_path = tmp_path / "stock_ai_candidate_snapshot_audit_latest.json"
    audit_path.write_text(json.dumps(_audit(), ensure_ascii=False), encoding="utf-8")

    paths = write_gap_triage_outputs(audit_path, output_dir=tmp_path)

    assert {path.name for path in paths} == {
        "stock_ai_candidate_gap_triage_latest.json",
        "stock_ai_candidate_gap_triage_20260528.json",
        "stock_ai_candidate_gap_triage_latest.csv",
        "stock_ai_candidate_gap_triage_20260528.csv",
        "stock_ai_candidate_gap_triage_summary_latest.json",
        "stock_ai_candidate_gap_triage_summary_20260528.json",
        "stock_ai_candidate_gap_triage_summary_latest.csv",
        "stock_ai_candidate_gap_triage_summary_20260528.csv",
        "stock_ai_candidate_gap_triage_audit_latest.json",
        "stock_ai_candidate_gap_triage_audit_20260528.json",
        "stock_ai_candidate_gap_triage_audit_latest.csv",
        "stock_ai_candidate_gap_triage_audit_20260528.csv",
        "stock_ai_candidate_gap_triage_latest.md",
        "stock_ai_candidate_gap_triage_20260528.md",
    }
    rows = json.loads((tmp_path / "stock_ai_candidate_gap_triage_latest.json").read_text(encoding="utf-8"))
    assert rows[0]["gap_id"] == "quote:realtime_quote_missing"
    summary = json.loads((tmp_path / "stock_ai_candidate_gap_triage_summary_latest.json").read_text(encoding="utf-8"))
    assert summary[0]["action_bucket"] == "realtime_quote_source_gap"
    assert "realtime_quote_source_gap" in (
        tmp_path / "stock_ai_candidate_gap_triage_summary_latest.csv"
    ).read_text(encoding="utf-8-sig")
    audit = json.loads((tmp_path / "stock_ai_candidate_gap_triage_audit_latest.json").read_text(encoding="utf-8"))
    assert audit["overall_status"] == "WARN"
    assert "gap_triage_summary_consistency" in (
        tmp_path / "stock_ai_candidate_gap_triage_audit_latest.csv"
    ).read_text(encoding="utf-8-sig")
    markdown = (tmp_path / "stock_ai_candidate_gap_triage_latest.md").read_text(encoding="utf-8")
    assert "### AI Snapshot Data Gap Triage" in markdown
    assert "| Audit | Status | Detail Rows | Summary Rows | Checks |" in markdown
    assert "| Gap triage self-audit | WARN | 2 | 2 | WARN:1, PASS:5 |" in markdown
    assert "| Severity | Action Bucket | Gaps | Affected Rows | Blocks | Action State |" in markdown
    assert "realtime_quote_source_gap" in markdown


def test_render_gap_triage_markdown_handles_empty_and_missing_audit() -> None:
    assert "No weak data coverage gaps found" in render_gap_triage_markdown([], audit={})
    assert "SKIPPED" in render_gap_triage_markdown([], audit=None)


def test_gap_triage_cli_writes_outputs(tmp_path) -> None:
    audit_path = tmp_path / "stock_ai_candidate_snapshot_audit_latest.json"
    audit_path.write_text(json.dumps(_audit(), ensure_ascii=False), encoding="utf-8")

    assert gap_triage_main(["--snapshot-audit-path", str(audit_path), "--output-dir", str(tmp_path)]) == 0
    assert (tmp_path / "stock_ai_candidate_gap_triage_latest.json").exists()
    assert (tmp_path / "stock_ai_candidate_gap_triage_summary_latest.json").exists()
    assert (tmp_path / "stock_ai_candidate_gap_triage_audit_latest.json").exists()
