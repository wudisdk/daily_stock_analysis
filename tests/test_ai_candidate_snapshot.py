# -*- coding: utf-8 -*-
"""Tests for low-sensitivity AI candidate snapshot exports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from src.services.ai_candidate_snapshot import (
    build_ai_candidate_snapshot_rows,
    write_ai_candidate_snapshot_files,
)


def _result(code: str = "600519", score: int = 72) -> SimpleNamespace:
    return SimpleNamespace(
        code=code,
        name="贵州茅台",
        sentiment_score=score,
        decision_type="hold",
        confidence_level="中",
        operation_advice="观察",
        report_language="zh",
        model_used="deepseek/deepseek-v4-pro",
        query_id=f"q-{code}",
        success=True,
        diagnostic_context_snapshot={
            "enhanced_context": {
                "code": code,
                "stock_name": "贵州茅台",
                "date": "2026-05-28",
                "today": {"date": "2026-05-28", "close": 1880.0},
                "yesterday": {"date": "2026-05-27", "close": 1870.0},
                "realtime": {
                    "price": 1880.0,
                    "change_60d": 31.2,
                    "volume_ratio": 2.4,
                    "turnover_rate": 5.5,
                    "api_key": "should-redact",
                },
                "trend_analysis": {
                    "signal_score": score,
                    "bias_ma5": 6.2,
                    "risk_factors": ["raw risk text should stay out"],
                },
                "fundamental_context": {
                    "status": "ok",
                    "coverage": {
                        "valuation": "ok",
                        "growth": "partial",
                        "earnings": "ok",
                        "capital_flow": "ok",
                    },
                    "source_chain": [{"provider": "fundamental_pipeline"}],
                    "capital_flow": {
                        "status": "ok",
                        "data": {
                            "stock_flow": {
                                "main_net_inflow": -1_000_000.0,
                                "inflow_5d": -2_000_000.0,
                            }
                        },
                    },
                },
            },
            "news_content": "完整新闻正文不应进入候选快照",
            "news_retrieval_content": "检索正文也不应进入候选快照",
            "news_result_count": 4,
            "realtime_quote_raw": {
                "source": "akshare_em",
                "price": 1880.0,
                "change_60d": 31.2,
                "volume_ratio": 2.4,
                "turnover_rate": 5.5,
            },
        },
    )


def test_build_snapshot_rows_exports_ranked_low_sensitivity_dimensions() -> None:
    rows = build_ai_candidate_snapshot_rows(
        [_result("000001", 62), _result("600519", 82), None],
        created_at=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
        run_id="run-1",
    )

    assert [row["stock_code"] for row in rows] == ["600519", "000001"]
    assert rows[0]["candidate_rank"] == 1
    assert rows[0]["model_used"] == "deepseek/deepseek-v4-pro"
    dimensions = {
        item["name"]: item
        for item in rows[0]["factor_snapshot"]["dimensions"]
    }
    assert dimensions["technical_score"]["label"] == "strong"
    assert dimensions["price_heat"]["label"] == "overheated"
    assert dimensions["fund_flow"]["label"] == "risk_guard"
    assert rows[0]["data_coverage"]["factor_snapshot"]["status"] == "available"
    assert rows[0]["data_coverage"]["news"]["status"] == "available"
    assert "news_content_omitted_low_sensitivity" in rows[0]["data_coverage"]["news"]["warnings"]
    assert rows[0]["news_result_count"] == 4

    dumped = json.dumps(rows, ensure_ascii=False)
    assert "完整新闻正文不应进入候选快照" not in dumped
    assert "检索正文也不应进入候选快照" not in dumped
    assert "1000000" not in dumped
    assert "raw risk text" not in dumped
    assert "should-redact" not in dumped


def test_build_snapshot_rows_uses_code_order_for_equal_scores_and_top_level_fallbacks() -> None:
    first = _result("600001", 72)
    second = _result("000001", 72)
    for result in (first, second):
        snapshot = result.diagnostic_context_snapshot
        enhanced = snapshot["enhanced_context"]
        snapshot["realtime_quote"] = snapshot.pop("realtime_quote_raw")
        snapshot["trend_result"] = enhanced.pop("trend_analysis")
        snapshot["fundamental_context"] = enhanced.pop("fundamental_context")
        enhanced.pop("realtime")

    rows = build_ai_candidate_snapshot_rows(
        [first, second],
        created_at=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
    )

    assert [row["stock_code"] for row in rows] == ["000001", "600001"]
    dimensions = {
        item["name"]: item
        for item in rows[0]["factor_snapshot"]["dimensions"]
    }
    assert dimensions["technical_score"]["label"] == "constructive"
    assert dimensions["fund_flow"]["status"] == "available"


def test_write_snapshot_files_writes_latest_and_trade_date_jsonl(tmp_path) -> None:
    paths = write_ai_candidate_snapshot_files(
        [_result()],
        output_dir=tmp_path,
        created_at=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
        run_id="run-1",
    )

    assert {path.name for path in paths} == {
        "stock_ai_candidate_snapshot_latest.jsonl",
        "stock_ai_candidate_snapshot_20260528.jsonl",
    }
    latest = tmp_path / "stock_ai_candidate_snapshot_latest.jsonl"
    row = json.loads(latest.read_text(encoding="utf-8").strip())
    assert row["schema_version"] == "1.0"
    assert row["snapshot_kind"] == "post_analysis_candidate"


def test_snapshot_export_skips_failed_or_empty_results() -> None:
    failed = _result()
    failed.success = False

    assert build_ai_candidate_snapshot_rows([None, failed]) == []
