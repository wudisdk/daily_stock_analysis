# -*- coding: utf-8 -*-
"""Tests for hosted AI candidate snapshot audit artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.services.ai_snapshot_audit import audit_snapshot, write_audit_outputs


def _row(
    stock_code: str = "688981",
    *,
    rank: int = 1,
    trade_date: str = "2026-05-28",
    model_used: str = "deepseek/deepseek-v4-pro",
    digest: str = "a" * 64,
) -> dict:
    dimensions = [
        {"name": "technical_score", "status": "available", "label": "strong"},
        {"name": "price_heat", "status": "available", "label": "normal"},
        {"name": "volume_price", "status": "available", "label": "active"},
        {"name": "industry_theme", "status": "available", "label": "membership_available"},
        {"name": "valuation", "status": "available", "label": "available"},
        {"name": "quality_growth", "status": "available", "label": "available"},
        {"name": "fund_flow", "status": "not_supported", "label": "not_supported"},
        {"name": "de_risk", "status": "not_supported", "label": "not_supported"},
        {"name": "data_coverage", "status": "available", "label": "high"},
        {"name": "risk", "status": "available", "label": "no_risk_flags"},
        {"name": "confidence", "status": "available", "label": "high"},
    ]
    return {
        "schema_version": "1.1",
        "snapshot_kind": "post_analysis_candidate",
        "created_at": "2026-05-28T16:00:00+00:00",
        "candidate_source": "daily_analysis",
        "candidate_rank": rank,
        "trade_date": trade_date,
        "stock_code": stock_code,
        "model_used": model_used,
        "input_snapshot_hash": digest,
        "factor_snapshot": {
            "status": "available",
            "dimensions": dimensions,
            "derived_from": ["quote", "technical", "fundamentals"],
        },
        "data_coverage": {
            "quote": {"status": "available"},
            "daily_bars": {"status": "available"},
            "technical": {"status": "available"},
            "fundamentals": {"status": "available"},
            "factor_snapshot": {"status": "available"},
            "news": {"status": "available"},
        },
        "news_result_count": 3,
    }


def _write_jsonl(path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_audit_snapshot_passes_valid_low_sensitivity_rows(tmp_path) -> None:
    snapshot_dir = tmp_path / "reports" / "ai_snapshot"
    snapshot_dir.mkdir(parents=True)
    latest_path = snapshot_dir / "stock_ai_candidate_snapshot_latest.jsonl"
    dated_path = snapshot_dir / "stock_ai_candidate_snapshot_20260528.jsonl"
    rows = [
        _row("HK00981", rank=1, trade_date="2026-05-27", digest="b" * 64),
        _row("688981", rank=2, trade_date="2026-05-28", digest="c" * 64),
    ]
    _write_jsonl(latest_path, rows)
    _write_jsonl(dated_path, rows)

    audit = audit_snapshot(
        latest_path,
        created_at=datetime(2026, 5, 28, 16, 0, tzinfo=timezone.utc),
    )

    assert audit["overall_status"] == "PASS"
    assert audit["row_count"] == 2
    assert audit["latest_trade_date"] == "2026-05-28"
    checks = {check["check_id"]: check for check in audit["checks"]}
    assert checks["input_snapshot_hash"]["status"] == "PASS"
    assert checks["dated_snapshot_latest_trade_date"]["status"] == "PASS"
    assert checks["model_used_deepseek_pro"]["status"] == "PASS"


def test_write_audit_outputs_writes_latest_and_dated_json_csv(tmp_path) -> None:
    audit = {
        "generated_at": "2026-05-28T16:00:00+00:00",
        "latest_trade_date": "2026-05-28",
        "overall_status": "PASS",
        "status_counts": {"PASS": 1},
        "row_count": 1,
        "checks": [
            {
                "check_id": "snapshot_rows_non_empty",
                "status": "PASS",
                "message": "ok",
                "evidence": {"row_count": 1},
            }
        ],
    }

    paths = write_audit_outputs(audit, tmp_path)

    assert {path.name for path in paths} == {
        "stock_ai_candidate_snapshot_audit_latest.json",
        "stock_ai_candidate_snapshot_audit_20260528.json",
        "stock_ai_candidate_snapshot_audit_latest.csv",
        "stock_ai_candidate_snapshot_audit_20260528.csv",
    }
    assert json.loads((tmp_path / "stock_ai_candidate_snapshot_audit_latest.json").read_text(encoding="utf-8"))[
        "overall_status"
    ] == "PASS"
    assert "snapshot_rows_non_empty" in (
        tmp_path / "stock_ai_candidate_snapshot_audit_latest.csv"
    ).read_text(encoding="utf-8-sig")


def test_audit_snapshot_fails_sensitive_flash_and_missing_dated_file(tmp_path) -> None:
    snapshot_dir = tmp_path / "reports" / "ai_snapshot"
    snapshot_dir.mkdir(parents=True)
    latest_path = snapshot_dir / "stock_ai_candidate_snapshot_latest.jsonl"
    bad_row = _row(
        "688981",
        model_used="gemini/gemini-2.5-flash",
        digest="not-a-hash",
    )
    bad_row["api_key"] = "sk-abcdefghijklmnop"
    bad_row["factor_snapshot"]["dimensions"] = [
        item
        for item in bad_row["factor_snapshot"]["dimensions"]
        if item["name"] != "industry_theme"
    ]
    _write_jsonl(latest_path, [bad_row])

    audit = audit_snapshot(latest_path)

    assert audit["overall_status"] == "FAIL"
    checks = {check["check_id"]: check for check in audit["checks"]}
    assert checks["input_snapshot_hash"]["status"] == "FAIL"
    assert checks["model_used_deepseek_pro"]["status"] == "FAIL"
    assert checks["low_sensitivity_boundary"]["status"] == "FAIL"
    assert checks["factor_snapshot_required_dimensions"]["status"] == "FAIL"
    assert checks["dated_snapshot_latest_trade_date"]["status"] == "FAIL"


def test_audit_snapshot_warns_on_missing_critical_coverage_blocks(tmp_path) -> None:
    snapshot_dir = tmp_path / "reports" / "ai_snapshot"
    snapshot_dir.mkdir(parents=True)
    latest_path = snapshot_dir / "stock_ai_candidate_snapshot_latest.jsonl"
    dated_path = snapshot_dir / "stock_ai_candidate_snapshot_20260528.jsonl"
    row = _row("688981")
    row["data_coverage"] = {
        "quote": {"status": "missing", "missing_reasons": ["realtime_quote_missing"]},
        "daily_bars": {"status": "missing", "missing_reasons": ["today_missing"]},
        "technical": {"status": "available"},
        "factor_snapshot": {"status": "missing", "missing_reasons": ["technical_score_missing"]},
        "news": {"status": "missing", "missing_reasons": ["news_context_missing"]},
    }
    _write_jsonl(latest_path, [row])
    _write_jsonl(dated_path, [row])

    audit = audit_snapshot(latest_path)

    assert audit["overall_status"] == "WARN"
    checks = {check["check_id"]: check for check in audit["checks"]}
    coverage_check = checks["data_coverage_block_status"]
    assert coverage_check["status"] == "WARN"
    assert coverage_check["evidence"]["block_status_counts"]["quote"]["missing"] == 1
    weak_blocks = {
        item["block"]
        for item in coverage_check["evidence"]["weak_block_rows"]
    }
    assert {"quote", "daily_bars", "factor_snapshot"} <= weak_blocks
    critical_blocks = {
        item["block"]
        for item in coverage_check["evidence"]["critical_weak_block_rows"]
    }
    optional_blocks = {
        item["block"]
        for item in coverage_check["evidence"]["optional_weak_block_rows"]
    }
    assert {"quote", "daily_bars", "factor_snapshot"} <= critical_blocks
    assert {"fundamentals", "news"} <= optional_blocks


def test_audit_snapshot_tracks_optional_coverage_gaps_without_warning(tmp_path) -> None:
    snapshot_dir = tmp_path / "reports" / "ai_snapshot"
    snapshot_dir.mkdir(parents=True)
    latest_path = snapshot_dir / "stock_ai_candidate_snapshot_latest.jsonl"
    dated_path = snapshot_dir / "stock_ai_candidate_snapshot_20260528.jsonl"
    row = _row("688981")
    row["data_coverage"] = {
        "quote": {"status": "available"},
        "daily_bars": {"status": "available"},
        "technical": {"status": "available"},
        "factor_snapshot": {"status": "available"},
        "fundamentals": {"status": "missing", "missing_reasons": ["fundamental_context_missing"]},
        "news": {"status": "missing", "missing_reasons": ["news_context_missing"]},
    }
    _write_jsonl(latest_path, [row])
    _write_jsonl(dated_path, [row])

    audit = audit_snapshot(latest_path)

    assert audit["overall_status"] == "PASS"
    checks = {check["check_id"]: check for check in audit["checks"]}
    coverage_check = checks["data_coverage_block_status"]
    assert coverage_check["status"] == "PASS"
    assert coverage_check["evidence"]["critical_weak_block_rows"] == []
    optional_blocks = {
        item["block"]
        for item in coverage_check["evidence"]["optional_weak_block_rows"]
    }
    assert optional_blocks == {"fundamentals", "news"}
    weak_blocks = {
        item["block"]
        for item in coverage_check["evidence"]["weak_block_rows"]
    }
    assert weak_blocks == {"fundamentals", "news"}
