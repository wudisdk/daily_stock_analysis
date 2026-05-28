# -*- coding: utf-8 -*-
"""Tests for the Issue #1389 P2 AnalysisContextPack assembler."""

from __future__ import annotations

import builtins
import importlib
from dataclasses import dataclass

import pytest

from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.schemas.analysis_context_pack import ContextFieldStatus
import src.services.analysis_context_builder as builder_module
from src.services.analysis_context_builder import (
    AnalysisContextBuilder,
    PipelineAnalysisArtifacts,
)


@dataclass
class _FakeTrend:
    data: dict

    def to_dict(self) -> dict:
        return dict(self.data)


@dataclass
class _FakeChip:
    data: dict

    def to_dict(self) -> dict:
        return dict(self.data)


class _BrokenTrend:
    def to_dict(self) -> dict:
        raise RuntimeError("broken trend artifact")


class _InvalidTrend:
    def to_dict(self) -> list:
        return ["not", "a", "mapping"]


def _quote(source: RealtimeSource = RealtimeSource.AKSHARE_EM) -> UnifiedRealtimeQuote:
    return UnifiedRealtimeQuote(
        code="600519",
        name="贵州茅台",
        source=source,
        price=1880.0,
        change_pct=1.2,
        volume_ratio=1.3,
        turnover_rate=0.5,
    )


def _artifacts(**overrides) -> PipelineAnalysisArtifacts:
    data = {
        "code": "600519",
        "stock_name": "贵州茅台",
        "market": "cn",
        "phase": {"market": "cn", "phase": "intraday"},
        "base_context": {
            "code": "600519",
            "stock_name": "贵州茅台",
            "date": "2026-05-24",
            "today": {"date": "2026-05-24", "close": 1880.0},
            "yesterday": {"date": "2026-05-23", "close": 1860.0},
        },
        "enhanced_context": {
            "today": {"date": "2026-05-24", "close": 1880.0},
        },
        "realtime_quote": _quote(),
        "trend_result": _FakeTrend(
            {
                "trend_status": "多头排列",
                "ma5": 1800.0,
                "ma10": 1780.0,
                "rsi_6": 66.0,
            }
        ),
        "chip_data": _FakeChip(
            {
                "code": "600519",
                "date": "2026-05-24",
                "source": "akshare",
                "profit_ratio": 0.72,
                "avg_cost": 1700.0,
            }
        ),
        "fundamental_context": {
            "status": "ok",
            "coverage": {"valuation": "ok"},
            "source_chain": [{"provider": "fundamental_pipeline", "result": "ok"}],
        },
        "news_context": "公司公告与行业新闻摘要",
        "news_result_count": 3,
        "metadata": {"query_id": "q-1", "trigger_source": "api"},
    }
    data.update(overrides)
    return PipelineAnalysisArtifacts(**data)


def test_quote_block_maps_available_missing_fallback_and_explicit_stale() -> None:
    available = AnalysisContextBuilder.build(_artifacts()).blocks["quote"]
    assert available.status == ContextFieldStatus.AVAILABLE
    assert available.source == "akshare_em"
    assert available.items["price"].value == 1880.0

    yfinance = AnalysisContextBuilder.build(
        _artifacts(realtime_quote=_quote(RealtimeSource.YFINANCE))
    ).blocks["quote"]
    assert yfinance.status == ContextFieldStatus.AVAILABLE
    assert yfinance.source == "yfinance"
    assert "realtime_provider_fallback" not in yfinance.warnings

    missing = AnalysisContextBuilder.build(
        _artifacts(realtime_quote=None)
    ).blocks["quote"]
    assert missing.status == ContextFieldStatus.MISSING
    assert missing.items["quote"].missing_reason == "realtime_quote_missing"

    fallback = AnalysisContextBuilder.build(
        _artifacts(realtime_quote=_quote(RealtimeSource.FALLBACK))
    ).blocks["quote"]
    assert fallback.status == ContextFieldStatus.FALLBACK
    assert "realtime_provider_fallback" in fallback.warnings
    assert fallback.items["price"].fallback_from is None

    explicit_fallback = AnalysisContextBuilder.build(
        _artifacts(
            realtime_quote={
                "source": "fallback",
                "price": 1870.0,
                "fallback_from": "primary_realtime_provider",
            }
        )
    ).blocks["quote"]
    assert explicit_fallback.status == ContextFieldStatus.FALLBACK
    assert explicit_fallback.items["price"].fallback_from == "primary_realtime_provider"
    assert "realtime_provider_fallback" not in explicit_fallback.warnings

    stale = AnalysisContextBuilder.build(
        _artifacts(metadata={"query_id": "q-1", "price_stale": True})
    ).blocks["quote"]
    assert stale.status == ContextFieldStatus.STALE
    assert stale.metadata["price_stale"] is True
    assert "quote_stale" in stale.warnings


def test_daily_bars_uses_base_context_and_keeps_dates_out_of_timestamp() -> None:
    pack = AnalysisContextBuilder.build(
        _artifacts(
            base_context={
                "code": "600519",
                "stock_name": "贵州茅台",
                "date": "2026-05-24",
                "data_missing": True,
                "today": {},
                "yesterday": {},
            },
            enhanced_context={
                "today": {
                    "date": "2026-05-26",
                    "close": 1900.0,
                    "data_source": "realtime:akshare_em",
                }
            },
        )
    )

    block = pack.blocks["daily_bars"]
    dumped = block.model_dump(mode="json")

    assert block.status == ContextFieldStatus.MISSING
    assert block.metadata["date"] == "2026-05-24"
    assert all(item["timestamp"] is None for item in dumped["items"].values())
    assert dumped["items"]["today"]["metadata"]["date"] == "2026-05-24"

    date_only = AnalysisContextBuilder.build(
        _artifacts(
            base_context={
                "date": "2026-05-24",
                "today": {},
                "yesterday": {},
            }
        )
    ).blocks["daily_bars"]
    assert date_only.status == ContextFieldStatus.MISSING
    assert date_only.items["date"].status == ContextFieldStatus.AVAILABLE

    one_bar = AnalysisContextBuilder.build(
        _artifacts(
            base_context={
                "date": "2026-05-24",
                "today": {"date": "2026-05-24", "close": 1880.0},
                "yesterday": {},
            }
        )
    ).blocks["daily_bars"]
    assert one_bar.status == ContextFieldStatus.PARTIAL


def test_technical_missing_and_realtime_overlay_statuses_are_explicit() -> None:
    missing = AnalysisContextBuilder.build(
        _artifacts(trend_result=None)
    ).blocks["technical"]
    assert missing.status == ContextFieldStatus.MISSING
    assert missing.items["trend_result"].missing_reason == "trend_result_missing"

    pack = AnalysisContextBuilder.build(
        _artifacts(
            enhanced_context={
                "today": {
                    "close": 1880.0,
                    "data_source": "realtime:akshare_em",
                }
            }
        )
    )
    block = pack.blocks["technical"]

    assert block.status == ContextFieldStatus.PARTIAL
    assert block.items["trend_result"].status == ContextFieldStatus.AVAILABLE
    assert block.items["intraday_overlay"].status == ContextFieldStatus.ESTIMATED
    assert "intraday_realtime_overlay" in block.warnings
    assert "intraday_realtime_overlay" in pack.data_quality.warnings


def test_chip_missing_defaults_to_missing_and_explicit_not_supported() -> None:
    missing = AnalysisContextBuilder.build(_artifacts(chip_data=None)).blocks["chip"]
    assert missing.status == ContextFieldStatus.MISSING
    assert (
        missing.items["chip_distribution"].missing_reason
        == "chip_distribution_missing"
    )

    not_supported = AnalysisContextBuilder.build(
        _artifacts(chip_data=None, metadata={"chip_not_supported": True})
    ).blocks["chip"]
    assert not_supported.status == ContextFieldStatus.NOT_SUPPORTED
    assert (
        not_supported.items["chip_distribution"].missing_reason
        == "chip_not_supported"
    )


@pytest.mark.parametrize(
    ("payload_status", "expected_status"),
    (
        ("ok", ContextFieldStatus.AVAILABLE),
        ("not_supported", ContextFieldStatus.NOT_SUPPORTED),
        ("partial", ContextFieldStatus.PARTIAL),
        ("failed", ContextFieldStatus.MISSING),
    ),
)
def test_fundamentals_maps_supported_statuses_without_raw_errors(
    payload_status: str,
    expected_status: ContextFieldStatus,
) -> None:
    block = AnalysisContextBuilder.build(
        _artifacts(
            fundamental_context={
                "status": payload_status,
                "coverage": {"valuation": payload_status},
                "source_chain": [
                    {"provider": "fundamental_pipeline", "result": payload_status}
                ],
                "errors": ["token=secret should not be persisted"],
            }
        )
    ).blocks["fundamentals"]

    assert block.status == expected_status
    assert block.metadata["coverage"] == {"valuation": payload_status}
    assert block.items["coverage"].status == expected_status
    assert block.items["source_chain"].status == expected_status
    assert "errors" not in block.metadata
    assert "token=secret" not in str(block.model_dump(mode="json"))
    if payload_status == "failed":
        assert block.items["status"].missing_reason == "fundamental_pipeline_failed"
        assert block.items["coverage"].missing_reason == "fundamental_pipeline_failed"
        assert (
            block.items["source_chain"].missing_reason
            == "fundamental_pipeline_failed"
        )


def test_fundamentals_derives_capital_flow_guard_warnings() -> None:
    pack = AnalysisContextBuilder.build(
        _artifacts(
            realtime_quote={
                "source": "akshare_em",
                "price": 1880.0,
                "change_60d": 31.2,
                "volume_ratio": 2.4,
            },
            fundamental_context={
                "status": "ok",
                "coverage": {"valuation": "ok", "capital_flow": "ok"},
                "source_chain": [{"provider": "fundamental_pipeline", "result": "ok"}],
                "capital_flow": {
                    "status": "ok",
                    "data": {
                        "stock_flow": {
                            "main_net_inflow": -1_000_000.0,
                            "inflow_5d": -2_000_000.0,
                            "inflow_10d": None,
                        }
                    },
                },
            },
        )
    )

    block = pack.blocks["fundamentals"]

    assert "capital_flow_broke_proxy" in block.warnings
    assert "price_flow_hot_without_confirmed_inflow" in block.warnings
    assert block.metadata["guard_warnings"] == block.warnings
    assert block.items["capital_flow_guard"].value == {"warnings": block.warnings}
    assert "capital_flow_broke_proxy" in pack.data_quality.warnings


def test_fundamentals_marks_conflicting_capital_flow_without_hot_price_guard() -> None:
    pack = AnalysisContextBuilder.build(
        _artifacts(
            realtime_quote={
                "source": "akshare_em",
                "price": 1880.0,
                "change_60d": 31.2,
                "volume_ratio": 2.4,
            },
            fundamental_context={
                "status": "ok",
                "coverage": {"valuation": "ok", "capital_flow": "ok"},
                "source_chain": [{"provider": "fundamental_pipeline", "result": "ok"}],
                "capital_flow": {
                    "status": "ok",
                    "data": {
                        "stock_flow": {
                            "main_net_inflow": -1_000_000.0,
                            "inflow_5d": 2_000_000.0,
                            "inflow_10d": None,
                        }
                    },
                },
            },
        )
    )

    block = pack.blocks["fundamentals"]

    assert block.warnings == ["capital_flow_conflicting_signals"]
    assert "price_flow_hot_without_confirmed_inflow" not in block.warnings


def test_fundamentals_uses_trend_activity_for_price_flow_hot_guard() -> None:
    pack = AnalysisContextBuilder.build(
        _artifacts(
            realtime_quote={
                "source": "yfinance",
                "price": 1880.0,
                "volume_ratio": None,
                "change_60d": None,
            },
            trend_result=_FakeTrend(
                {
                    "bias_ma5": 6.4,
                    "volume_ratio_5d": 2.2,
                    "signal_score": 64,
                }
            ),
            fundamental_context={
                "status": "ok",
                "coverage": {"valuation": "ok", "capital_flow": "ok"},
                "source_chain": [{"provider": "fundamental_pipeline", "result": "ok"}],
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
        )
    )

    fundamentals = pack.blocks["fundamentals"]
    factor_snapshot = pack.blocks["factor_snapshot"]
    dimensions = {item["name"]: item for item in factor_snapshot.metadata["dimensions"]}

    assert "capital_flow_broke_proxy" in fundamentals.warnings
    assert "price_flow_hot_without_confirmed_inflow" in fundamentals.warnings
    assert dimensions["de_risk"]["label"] == "flow_broke_price_flow_hot"


def test_factor_snapshot_derives_low_sensitivity_duckdb_style_dimensions() -> None:
    pack = AnalysisContextBuilder.build(
        _artifacts(
            realtime_quote={
                "source": "akshare_em",
                "price": 1880.0,
                "change_60d": 31.2,
                "volume_ratio": 2.4,
                "turnover_rate": 5.5,
            },
            trend_result=_FakeTrend(
                {
                    "signal_score": 72,
                    "bias_ma5": 6.2,
                    "volume_ratio_5d": 1.8,
                    "risk_factors": ["raw risk text should stay out"],
                }
            ),
            fundamental_context={
                "status": "ok",
                "coverage": {
                    "valuation": "ok",
                    "growth": "partial",
                    "earnings": "ok",
                    "capital_flow": "ok",
                    "boards": "ok",
                },
                "source_chain": [{"provider": "fundamental_pipeline", "result": "ok"}],
                "belong_boards": [{"name": "半导体", "type": "行业"}],
                "boards": {
                    "status": "ok",
                    "data": {
                        "top": [{"name": "半导体", "change_pct": 2.5}],
                        "bottom": [{"name": "煤炭", "change_pct": -1.5}],
                    },
                },
                "capital_flow": {
                    "status": "ok",
                    "data": {
                        "stock_flow": {
                            "main_net_inflow": -1_000_000.0,
                            "inflow_5d": -2_000_000.0,
                            "inflow_10d": None,
                        }
                    },
                },
            },
        )
    )

    block = pack.blocks["factor_snapshot"]
    dimensions = {item["name"]: item for item in block.metadata["dimensions"]}

    assert block.status == ContextFieldStatus.AVAILABLE
    assert dimensions["technical_score"]["label"] == "constructive"
    assert dimensions["price_heat"]["label"] == "overheated"
    assert dimensions["volume_price"]["label"] == "high_activity"
    assert dimensions["industry_theme"]["label"] == "theme_tailwind"
    assert dimensions["quality_growth"]["label"] == "available"
    assert dimensions["fund_flow"]["label"] == "risk_guard"
    assert dimensions["de_risk"]["label"] == "flow_broke_price_flow_hot"
    assert dimensions["data_coverage"]["label"] == "high"
    assert dimensions["risk"]["label"] == "has_risk_flags"
    assert dimensions["confidence"]["label"] == "medium"
    assert "capital_flow_broke_proxy" in block.warnings
    assert "factor_snapshot_price_overheated" in block.warnings
    assert "capital_flow_broke_proxy" in pack.data_quality.warnings
    dumped = str(block.model_dump(mode="json"))
    assert "1000000" not in dumped
    assert "raw risk text" not in dumped
    assert "半导体" not in dumped


def test_factor_snapshot_marks_industry_theme_not_supported_without_board_payload() -> None:
    block = AnalysisContextBuilder.build(
        _artifacts(
            fundamental_context={
                "status": "partial",
                "coverage": {"boards": "not_supported"},
                "boards": {"status": "not_supported", "data": {}},
            },
        )
    ).blocks["factor_snapshot"]

    dimensions = {item["name"]: item for item in block.metadata["dimensions"]}
    assert dimensions["industry_theme"]["status"] == "not_supported"
    assert dimensions["industry_theme"]["missing_reason"] == "industry_theme_not_supported"


def test_factor_snapshot_marks_industry_theme_from_membership_only() -> None:
    block = AnalysisContextBuilder.build(
        _artifacts(
            fundamental_context={
                "status": "partial",
                "coverage": {"boards": "failed"},
                "belong_boards": [{"name": "Semiconductors", "type": "industry"}],
                "boards": {"status": "failed", "data": {}},
            },
        )
    ).blocks["factor_snapshot"]

    dimensions = {item["name"]: item for item in block.metadata["dimensions"]}
    assert dimensions["industry_theme"]["status"] == "available"
    assert dimensions["industry_theme"]["label"] == "membership_available"


def test_factor_snapshot_marks_clear_de_risk_when_flow_inputs_are_present() -> None:
    block = AnalysisContextBuilder.build(
        _artifacts(
            realtime_quote={
                "source": "akshare_em",
                "price": 1880.0,
                "change_60d": 4.2,
                "volume_ratio": 1.1,
            },
            fundamental_context={
                "status": "ok",
                "coverage": {"capital_flow": "ok"},
                "source_chain": [{"provider": "fundamental_pipeline", "result": "ok"}],
                "capital_flow": {
                    "status": "ok",
                    "data": {
                        "stock_flow": {
                            "main_net_inflow": 1_000_000.0,
                            "inflow_5d": 2_000_000.0,
                        }
                    },
                },
            },
        )
    ).blocks["factor_snapshot"]

    dimensions = {item["name"]: item for item in block.metadata["dimensions"]}
    assert dimensions["fund_flow"]["label"] == "available"
    assert dimensions["de_risk"]["label"] == "clear"
    assert dimensions["data_coverage"]["label"] == "high"


def test_factor_snapshot_marks_missing_inputs_without_fetching() -> None:
    block = AnalysisContextBuilder.build(
        _artifacts(
            realtime_quote=None,
            trend_result=None,
            fundamental_context=None,
        )
    ).blocks["factor_snapshot"]

    assert block.status == ContextFieldStatus.MISSING
    assert (
        block.items["technical_score"].missing_reason == "technical_score_missing"
    )
    assert block.items["de_risk"].missing_reason == "de_risk_signal_missing"
    assert block.items["confidence"].missing_reason == "factor_snapshot_inputs_missing"


def test_builder_does_not_hide_broken_artifact_to_dict() -> None:
    with pytest.raises(RuntimeError, match="broken trend artifact"):
        AnalysisContextBuilder.build(_artifacts(trend_result=_BrokenTrend()))


def test_builder_rejects_non_mapping_artifact_to_dict() -> None:
    with pytest.raises(TypeError, match="to_dict\\(\\) must return a mapping"):
        AnalysisContextBuilder.build(_artifacts(trend_result=_InvalidTrend()))


def test_news_block_treats_blank_as_missing_and_records_pack_metadata() -> None:
    blank = AnalysisContextBuilder.build(
        _artifacts(news_context="  ", news_result_count=0)
    )
    assert blank.blocks["news"].status == ContextFieldStatus.MISSING
    assert blank.metadata["news_result_count"] == 0

    available = AnalysisContextBuilder.build(
        _artifacts(news_context="news", news_result_count=5)
    )
    assert available.blocks["news"].status == ContextFieldStatus.AVAILABLE
    assert available.blocks["news"].items["content"].value == "news"
    assert available.metadata["news_result_count"] == 5


def test_build_batch_returns_one_pack_per_artifact() -> None:
    packs = AnalysisContextBuilder.build_batch(
        [
            _artifacts(code="600519", stock_name="贵州茅台"),
            _artifacts(code="000001", stock_name="平安银行"),
        ]
    )

    assert [pack.subject.code for pack in packs] == ["600519", "000001"]


def test_builder_output_safe_dict_redacts_sensitive_mapping_keys() -> None:
    pack = AnalysisContextBuilder.build(
        _artifacts(
            metadata={
                "query_id": "q-1",
                "webhook_url": "https://hooks.example.test/secret",
            },
            fundamental_context={
                "status": "ok",
                "coverage": {
                    "valuation": "ok",
                    "access_token": "secret-token",
                },
                "source_chain": [{"provider": "fundamental_pipeline"}],
            },
        )
    )

    safe = pack.to_safe_dict()

    assert safe["metadata"]["webhook_url"] == "[REDACTED]"
    assert (
        safe["blocks"]["fundamentals"]["metadata"]["coverage"]["access_token"]
        == "[REDACTED]"
    )
    assert safe["blocks"]["fundamentals"]["metadata"]["coverage"]["valuation"] == "ok"


def test_builder_module_stays_zero_fetch_and_zero_storage_import(monkeypatch) -> None:
    forbidden_modules = (
        "data_provider",
        "fetcher_manager",
        "search_service",
        "src.storage",
        "src.services.search_service",
        "src.repositories",
        "src.database",
    )
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and any(
            name == module or name.startswith(f"{module}.")
            for module in forbidden_modules
        ):
            raise AssertionError(f"unexpected zero-fetch import: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    reloaded = importlib.reload(builder_module)
    pack = reloaded.AnalysisContextBuilder.build(_artifacts())

    assert pack.blocks["quote"].status == ContextFieldStatus.AVAILABLE
