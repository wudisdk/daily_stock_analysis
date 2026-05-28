# -*- coding: utf-8 -*-
"""Tests for AI candidate future-outcome replay queue artifacts."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

from src.services.ai_snapshot_replay_queue import (
    audit_replay_queue,
    build_replay_queue_rows,
    write_replay_queue_audit_outputs,
    write_replay_queue_outputs,
)


def _snapshot_row(
    code: str = "688981",
    *,
    rank: int = 1,
    trade_date: str = "2026-05-28",
    digest: str = "a" * 64,
) -> dict:
    return {
        "schema_version": "1.1",
        "snapshot_kind": "post_analysis_candidate",
        "candidate_source": "daily_analysis",
        "candidate_rank": rank,
        "trade_date": trade_date,
        "stock_code": code,
        "stock_name": "SMIC",
        "report_language": "zh",
        "model_used": "deepseek/deepseek-v4-pro",
        "model_result": {
            "sentiment_score": 68,
            "decision_type": "hold",
            "confidence_level": "high",
            "operation_advice": "watch",
        },
        "input_snapshot_hash": digest,
        "news_result_count": 4,
        "factor_snapshot": {
            "warnings": ["factor_snapshot_price_overheated"],
            "dimensions": [
                {"name": "technical_score", "status": "available", "label": "strong"},
                {"name": "price_heat", "status": "available", "label": "overheated"},
                {"name": "volume_price", "status": "available", "label": "high_activity"},
                {"name": "industry_theme", "status": "available", "label": "theme_tailwind"},
                {"name": "valuation", "status": "available", "label": "available"},
                {"name": "quality_growth", "status": "available", "label": "available"},
                {"name": "fund_flow", "status": "missing", "missing_reason": "fund_flow_snapshot_missing"},
                {"name": "de_risk", "status": "available", "label": "flow_broke_price_flow_hot"},
                {"name": "data_coverage", "status": "available", "label": "medium"},
                {"name": "risk", "status": "available", "label": "has_risk_flags"},
                {"name": "confidence", "status": "available", "label": "high"},
            ],
        },
        "data_coverage": {
            "news": {
                "status": "available",
                "warnings": ["news_content_omitted_low_sensitivity"],
            }
        },
    }


def _write_jsonl(path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_build_replay_queue_rows_expands_candidate_horizons_and_flags() -> None:
    rows = build_replay_queue_rows(
        [_snapshot_row()],
        generated_at=datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc),
    )

    assert [row["eval_horizon"] for row in rows] == ["1d", "3d", "1w", "1m", "1q"]
    assert [row["forward_trading_days"] for row in rows] == [1, 3, 5, 20, 60]
    assert {row["input_snapshot_hash"] for row in rows} == {"a" * 64}
    assert rows[0]["outcome_join_key"] == f"{'a' * 64}:1d"
    assert rows[0]["outcome_status"] == "pending_future_price"
    assert rows[0]["model_used"] == "deepseek/deepseek-v4-pro"
    assert rows[0]["model_sentiment_score"] == 68.0
    assert rows[0]["de_risk_label"] == "flow_broke_price_flow_hot"
    assert rows[0]["fund_flow_status"] == "missing"
    assert rows[0]["invalidation_flags"] == [
        "flow_broke_proxy",
        "fund_flow_missing",
        "hard_risk_hit_proxy",
        "price_flow_hot_proxy",
        "price_hot_20d_proxy",
    ]
    assert rows[0]["case_tags"] == [
        "flow_broke",
        "fund_flow_missing",
        "high_confidence",
        "price_flow_hot",
        "price_hot_20d",
        "quality_growth_available",
        "risk_flags",
        "technical_strong",
        "theme_tailwind",
    ]
    assert rows[0]["factor_warning_codes"] == [
        "factor_snapshot_price_overheated",
        "news_content_omitted_low_sensitivity",
    ]


def test_write_replay_queue_outputs_writes_latest_and_dated_jsonl_csv(tmp_path) -> None:
    snapshot_path = tmp_path / "stock_ai_candidate_snapshot_latest.jsonl"
    rows = [
        _snapshot_row("HK00981", rank=1, trade_date="2026-05-27", digest="b" * 64),
        _snapshot_row("688981", rank=2, trade_date="2026-05-28", digest="c" * 64),
    ]
    _write_jsonl(snapshot_path, rows)

    paths = write_replay_queue_outputs(
        snapshot_path,
        output_dir=tmp_path,
        generated_at=datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc),
    )

    assert {path.name for path in paths} == {
        "stock_ai_candidate_replay_queue_latest.jsonl",
        "stock_ai_candidate_replay_queue_20260528.jsonl",
        "stock_ai_candidate_replay_queue_latest.csv",
        "stock_ai_candidate_replay_queue_20260528.csv",
    }
    latest_rows = [
        json.loads(line)
        for line in (tmp_path / "stock_ai_candidate_replay_queue_latest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(latest_rows) == 10
    assert latest_rows[-1]["eval_horizon"] == "1q"
    assert latest_rows[-1]["stock_code"] == "688981"

    with (tmp_path / "stock_ai_candidate_replay_queue_latest.csv").open("r", encoding="utf-8-sig", newline="") as file:
        csv_rows = list(csv.DictReader(file))
    assert len(csv_rows) == 10
    assert json.loads(csv_rows[0]["invalidation_flags"]) == [
        "flow_broke_proxy",
        "fund_flow_missing",
        "hard_risk_hit_proxy",
        "price_flow_hot_proxy",
        "price_hot_20d_proxy",
    ]


def test_audit_replay_queue_passes_complete_low_sensitivity_queue(tmp_path) -> None:
    snapshot_rows = [
        _snapshot_row("HK00981", rank=1, trade_date="2026-05-27", digest="b" * 64),
        _snapshot_row("688981", rank=2, trade_date="2026-05-28", digest="c" * 64),
    ]
    queue_rows = build_replay_queue_rows(
        snapshot_rows,
        generated_at=datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc),
    )
    (tmp_path / "stock_ai_candidate_replay_queue_20260528.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "stock_ai_candidate_replay_queue_20260528.csv").write_text("", encoding="utf-8")

    audit = audit_replay_queue(
        snapshot_rows,
        queue_rows,
        output_dir=tmp_path,
        generated_at=datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc),
    )

    assert audit["overall_status"] == "PASS"
    assert audit["row_count"] == 10
    assert audit["candidate_count"] == 2
    assert audit["latest_trade_date"] == "2026-05-28"
    checks = {check["check_id"]: check for check in audit["checks"]}
    assert checks["replay_queue_candidate_horizon_coverage"]["status"] == "PASS"
    assert checks["replay_queue_join_keys"]["status"] == "PASS"
    assert checks["replay_queue_low_sensitivity_boundary"]["status"] == "PASS"

    paths = write_replay_queue_audit_outputs(audit, tmp_path)
    assert {path.name for path in paths} == {
        "stock_ai_candidate_replay_queue_audit_latest.json",
        "stock_ai_candidate_replay_queue_audit_20260528.json",
        "stock_ai_candidate_replay_queue_audit_latest.csv",
        "stock_ai_candidate_replay_queue_audit_20260528.csv",
    }


def test_audit_replay_queue_fails_bad_join_and_sensitive_payload(tmp_path) -> None:
    snapshot_rows = [_snapshot_row(digest="d" * 64)]
    queue_rows = build_replay_queue_rows(snapshot_rows)
    queue_rows = queue_rows[:-1]
    queue_rows[0]["outcome_join_key"] = "bad-key"
    queue_rows[0]["api_key"] = "sk-abcdefghijklmnop"

    audit = audit_replay_queue(snapshot_rows, queue_rows, output_dir=tmp_path)

    assert audit["overall_status"] == "FAIL"
    checks = {check["check_id"]: check for check in audit["checks"]}
    assert checks["replay_queue_candidate_horizon_coverage"]["status"] == "FAIL"
    assert checks["replay_queue_join_keys"]["status"] == "FAIL"
    assert checks["replay_queue_dated_files"]["status"] == "FAIL"
    assert checks["replay_queue_low_sensitivity_boundary"]["status"] == "FAIL"
