# -*- coding: utf-8 -*-
"""Tests for AI snapshot Step Summary rendering."""

from __future__ import annotations

import json

from src.services.ai_snapshot_step_summary import build_step_summary, main


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def test_build_step_summary_renders_audit_table_and_coverage_blocks(tmp_path) -> None:
    _write_json(
        tmp_path / "stock_ai_candidate_snapshot_audit_latest.json",
        {
            "overall_status": "WARN",
            "row_count": 2,
            "status_counts": {"PASS": 8, "WARN": 2},
            "checks": [
                {
                    "check_id": "data_coverage_block_status",
                    "status": "WARN",
                    "message": "critical data coverage blocks are missing or weak",
                    "evidence": {
                        "expected_blocks": ["quote", "daily_bars", "technical", "news"],
                        "critical_blocks": ["quote", "daily_bars", "technical"],
                        "block_status_counts": {
                            "quote": {"missing": 1, "available": 1},
                            "daily_bars": {"available": 2},
                            "technical": {"missing": 2},
                            "news": {"missing": 1, "available": 1},
                        },
                        "weak_block_rows": [
                            {
                                "row": {
                                    "stock_code": "688981",
                                    "candidate_rank": 1,
                                    "trade_date": "2026-05-28",
                                },
                                "block": "technical",
                                "status": "missing",
                                "missing_reasons": ["trend_result_missing"],
                            }
                        ],
                    },
                }
            ],
        },
    )
    _write_json(
        tmp_path / "stock_ai_candidate_replay_queue_audit_latest.json",
        {"overall_status": "PASS", "row_count": 10, "status_counts": {"PASS": 7}},
    )
    _write_json(
        tmp_path / "stock_ai_candidate_price_history_audit_latest.json",
        {"overall_status": "WARN", "row_count": 42, "status_counts": {"PASS": 4, "WARN": 1}},
    )
    _write_json(
        tmp_path / "stock_ai_candidate_gap_triage_audit_latest.json",
        {
            "overall_status": "WARN",
            "row_count": 11,
            "summary_row_count": 6,
            "status_counts": {"PASS": 5, "WARN": 1},
        },
    )

    summary = build_step_summary(tmp_path)

    assert "### AI Snapshot Artifacts" in summary
    assert "| Snapshot audit | WARN | 2 | WARN:2, PASS:8 |" in summary
    assert "| Replay queue audit | PASS | 10 | PASS:7 |" in summary
    assert "| Price history audit | WARN | 42 | WARN:1, PASS:4 |" in summary
    assert "| Gap triage audit | WARN | 11 | WARN:1, PASS:5 |" in summary
    assert "### AI Snapshot Data Coverage" in summary
    assert "| quote | missing:1, available:1 | yes |" in summary
    assert "| 688981 | 1 | 2026-05-28 | technical | missing | trend_result_missing |" in summary


def test_build_step_summary_handles_missing_artifacts(tmp_path) -> None:
    summary = build_step_summary(tmp_path)

    assert "| Snapshot audit | SKIPPED | 0 | missing `stock_ai_candidate_snapshot_audit_latest.json` |" in summary
    assert "- SKIPPED: missing snapshot audit artifact." in summary


def test_step_summary_cli_writes_output_file(tmp_path) -> None:
    output = tmp_path / "summary.md"

    assert main(["--input-dir", str(tmp_path), "--output-file", str(output)]) == 0
    assert output.exists()
    assert "AI Snapshot Artifacts" in output.read_text(encoding="utf-8")
