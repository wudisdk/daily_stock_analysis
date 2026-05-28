# -*- coding: utf-8 -*-
"""Assembler for the internal AnalysisContextPack P2 contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from src.schemas.analysis_context_pack import (
    AnalysisContextBlock,
    AnalysisContextItem,
    AnalysisContextPack,
    AnalysisSubject,
    ContextFieldStatus,
    DataQuality,
)


_REALTIME_OVERLAY_WARNING = "intraday_realtime_overlay"
_REALTIME_FALLBACK_WARNING = "realtime_provider_fallback"
_FUNDAMENTAL_FAILED_REASON = "fundamental_pipeline_failed"
_CAPITAL_FLOW_BROKE_PROXY_WARNING = "capital_flow_broke_proxy"
_CAPITAL_FLOW_CONFLICT_WARNING = "capital_flow_conflicting_signals"
_PRICE_FLOW_HOT_WITHOUT_INFLOW_WARNING = "price_flow_hot_without_confirmed_inflow"
_FACTOR_SNAPSHOT_SOURCE = "analysis_context_builder.factor_snapshot"
_FACTOR_PRICE_OVERHEATED_WARNING = "factor_snapshot_price_overheated"
_FACTOR_LOW_CONFIDENCE_WARNING = "factor_snapshot_low_confidence"


@dataclass(frozen=True)
class PipelineAnalysisArtifacts:
    """Artifacts already fetched by the stock analysis pipeline."""

    code: str
    stock_name: str
    market: str
    phase: Optional[Dict[str, Any]]
    base_context: Dict[str, Any]
    enhanced_context: Dict[str, Any]
    realtime_quote: Optional[Any]
    trend_result: Optional[Any]
    chip_data: Optional[Any]
    fundamental_context: Optional[Dict[str, Any]]
    news_context: Optional[str]
    news_result_count: Optional[int]
    metadata: Dict[str, Any]


class AnalysisContextBuilder:
    """Build AnalysisContextPack from existing pipeline artifacts only."""

    @staticmethod
    def build(artifacts: PipelineAnalysisArtifacts) -> AnalysisContextPack:
        metadata = dict(artifacts.metadata or {})
        if artifacts.news_result_count is not None:
            metadata["news_result_count"] = artifacts.news_result_count

        blocks: Dict[str, AnalysisContextBlock] = {}
        data_quality_warnings: List[str] = []

        blocks["quote"] = _build_quote_block(artifacts)
        blocks["daily_bars"] = _build_daily_bars_block(artifacts)
        technical_block, technical_warnings = _build_technical_block(artifacts)
        blocks["technical"] = technical_block
        data_quality_warnings.extend(technical_warnings)
        blocks["chip"] = _build_chip_block(artifacts)
        blocks["fundamentals"] = _build_fundamentals_block(artifacts)
        _extend_unique(data_quality_warnings, blocks["fundamentals"].warnings)
        blocks["factor_snapshot"] = _build_factor_snapshot_block(
            artifacts,
            capital_flow_warnings=blocks["fundamentals"].warnings,
        )
        _extend_unique(data_quality_warnings, blocks["factor_snapshot"].warnings)
        blocks["news"] = _build_news_block(artifacts)

        return AnalysisContextPack(
            subject=AnalysisSubject(
                code=artifacts.code,
                stock_name=artifacts.stock_name or None,
                market=artifacts.market or None,
            ),
            phase=artifacts.phase,
            blocks=blocks,
            data_quality=DataQuality(warnings=data_quality_warnings),
            metadata=metadata,
        )

    @staticmethod
    def build_batch(items: Sequence[PipelineAnalysisArtifacts]) -> List[AnalysisContextPack]:
        return [AnalysisContextBuilder.build(item) for item in items]


def _build_quote_block(artifacts: PipelineAnalysisArtifacts) -> AnalysisContextBlock:
    quote = _to_dict(artifacts.realtime_quote)
    if not quote:
        return AnalysisContextBlock(
            status=ContextFieldStatus.MISSING,
            items={
                "quote": AnalysisContextItem(
                    status=ContextFieldStatus.MISSING,
                    missing_reason="realtime_quote_missing",
                )
            },
        )

    source = _source_text(quote.get("source"))
    status = ContextFieldStatus.AVAILABLE
    warnings: List[str] = []
    fallback_from = _metadata_value(
        quote,
        "fallback_from",
        "quote_fallback_from",
        "realtime_fallback_from",
        "fallback_provider",
    ) or _metadata_value(
        artifacts.metadata,
        "quote_fallback_from",
        "realtime_fallback_from",
        "fallback_from",
    )

    if _has_explicit_quote_stale_marker(artifacts, quote):
        status = ContextFieldStatus.STALE
        warnings.append("quote_stale")
    elif source == "fallback":
        status = ContextFieldStatus.FALLBACK
        if fallback_from is None:
            warnings.append(_REALTIME_FALLBACK_WARNING)

    items = {
        key: AnalysisContextItem(
            status=status,
            value=value,
            source=source,
            fallback_from=fallback_from if status == ContextFieldStatus.FALLBACK else None,
            warnings=list(warnings),
        )
        for key, value in quote.items()
        if value is not None
    }
    return AnalysisContextBlock(
        status=status,
        items=items,
        source=source,
        warnings=warnings,
        metadata=_quote_metadata(artifacts, quote),
    )


def _build_daily_bars_block(artifacts: PipelineAnalysisArtifacts) -> AnalysisContextBlock:
    context = artifacts.base_context or {}
    date_value = context.get("date")
    metadata = {
        key: value
        for key, value in {
            "date": date_value,
            "data_missing": bool(context.get("data_missing")),
        }.items()
        if value not in (None, "")
    }
    if context.get("data_missing"):
        return AnalysisContextBlock(
            status=ContextFieldStatus.MISSING,
            items={
                "today": AnalysisContextItem(
                    status=ContextFieldStatus.MISSING,
                    value=context.get("today") or None,
                    missing_reason="daily_bars_missing",
                    metadata={"date": date_value} if date_value else {},
                ),
                "yesterday": AnalysisContextItem(
                    status=ContextFieldStatus.MISSING,
                    value=context.get("yesterday") or None,
                    missing_reason="daily_bars_missing",
                ),
            },
            source="storage.get_analysis_context",
            metadata=metadata,
        )

    items: Dict[str, AnalysisContextItem] = {}
    for key in ("today", "yesterday"):
        value = context.get(key)
        items[key] = AnalysisContextItem(
            status=ContextFieldStatus.AVAILABLE if value else ContextFieldStatus.MISSING,
            value=value or None,
            source="storage.get_analysis_context",
            missing_reason=None if value else f"{key}_missing",
        )
    if date_value:
        items["date"] = AnalysisContextItem(
            status=ContextFieldStatus.AVAILABLE,
            value=date_value,
            source="storage.get_analysis_context",
            metadata={"date": date_value},
        )

    bar_statuses = [items[key].status for key in ("today", "yesterday")]
    if all(status == ContextFieldStatus.AVAILABLE for status in bar_statuses):
        block_status = ContextFieldStatus.AVAILABLE
    elif any(status == ContextFieldStatus.AVAILABLE for status in bar_statuses):
        block_status = ContextFieldStatus.PARTIAL
    else:
        block_status = ContextFieldStatus.MISSING
    return AnalysisContextBlock(
        status=block_status,
        items=items,
        source="storage.get_analysis_context",
        metadata=metadata,
    )


def _build_technical_block(
    artifacts: PipelineAnalysisArtifacts,
) -> tuple[AnalysisContextBlock, List[str]]:
    trend = _to_dict(artifacts.trend_result)
    if not trend:
        return (
            AnalysisContextBlock(
                status=ContextFieldStatus.MISSING,
                items={
                    "trend_result": AnalysisContextItem(
                        status=ContextFieldStatus.MISSING,
                        missing_reason="trend_result_missing",
                    )
                },
            ),
            [],
        )

    has_realtime_overlay = _has_realtime_overlay(artifacts.enhanced_context)
    warnings = [_REALTIME_OVERLAY_WARNING] if has_realtime_overlay else []
    block_status = (
        ContextFieldStatus.PARTIAL
        if has_realtime_overlay
        else ContextFieldStatus.AVAILABLE
    )
    items: Dict[str, AnalysisContextItem] = {
        "trend_result": AnalysisContextItem(
            status=ContextFieldStatus.AVAILABLE,
            value=trend,
            warnings=list(warnings),
        )
    }
    if has_realtime_overlay:
        items["intraday_overlay"] = AnalysisContextItem(
            status=ContextFieldStatus.ESTIMATED,
            value=(artifacts.enhanced_context or {}).get("today"),
            warnings=list(warnings),
        )

    return (
        AnalysisContextBlock(
            status=block_status,
            items=items,
            warnings=warnings,
            metadata={
                "overlay_source": _realtime_overlay_source(artifacts.enhanced_context)
            },
        ),
        warnings,
    )


def _build_chip_block(artifacts: PipelineAnalysisArtifacts) -> AnalysisContextBlock:
    chip = _to_dict(artifacts.chip_data)
    if not chip:
        not_supported = bool((artifacts.metadata or {}).get("chip_not_supported"))
        status = (
            ContextFieldStatus.NOT_SUPPORTED
            if not_supported
            else ContextFieldStatus.MISSING
        )
        return AnalysisContextBlock(
            status=status,
            items={
                "chip_distribution": AnalysisContextItem(
                    status=status,
                    missing_reason=(
                        "chip_not_supported"
                        if not_supported
                        else "chip_distribution_missing"
                    ),
                )
            },
        )

    source = _source_text(chip.get("source"))
    return AnalysisContextBlock(
        status=ContextFieldStatus.AVAILABLE,
        items={
            key: AnalysisContextItem(
                status=ContextFieldStatus.AVAILABLE,
                value=value,
                source=source,
            )
            for key, value in chip.items()
            if value is not None
        },
        source=source,
        metadata={"date": chip.get("date")} if chip.get("date") else {},
    )


def _build_fundamentals_block(artifacts: PipelineAnalysisArtifacts) -> AnalysisContextBlock:
    context = artifacts.fundamental_context if isinstance(artifacts.fundamental_context, dict) else None
    if not context:
        return AnalysisContextBlock(
            status=ContextFieldStatus.MISSING,
            items={
                "fundamental_context": AnalysisContextItem(
                    status=ContextFieldStatus.MISSING,
                    missing_reason="fundamental_context_missing",
                )
            },
        )

    raw_status = str(context.get("status") or "").strip().lower()
    status = _fundamental_status(raw_status)
    missing_reason = (
        _FUNDAMENTAL_FAILED_REASON
        if raw_status == "failed"
        else ("fundamentals_not_supported" if raw_status == "not_supported" else None)
    )
    coverage = context.get("coverage") if isinstance(context.get("coverage"), dict) else {}
    source_chain = context.get("source_chain") if isinstance(context.get("source_chain"), list) else []
    source = _source_from_chain(source_chain)
    metadata = {
        "status": raw_status or None,
        "coverage": coverage,
        "source_chain": source_chain,
    }
    metadata = {key: value for key, value in metadata.items() if value not in (None, {}, [])}

    guard_warnings = _fundamental_guard_warnings(context, artifacts)
    if guard_warnings:
        metadata["guard_warnings"] = guard_warnings

    items = {
        "status": AnalysisContextItem(
            status=status,
            value=raw_status or None,
            source=source,
            missing_reason=missing_reason,
        ),
        "coverage": AnalysisContextItem(
            status=_fundamental_payload_status(status, bool(coverage)),
            value=coverage or None,
            source=source,
            missing_reason=_fundamental_payload_missing_reason(
                raw_status,
                bool(coverage),
                "fundamental_coverage_missing",
            ),
        ),
        "source_chain": AnalysisContextItem(
            status=_fundamental_payload_status(status, bool(source_chain)),
            value=source_chain or None,
            source=source,
            missing_reason=_fundamental_payload_missing_reason(
                raw_status,
                bool(source_chain),
                "fundamental_source_chain_missing",
            ),
        ),
    }
    if guard_warnings:
        items["capital_flow_guard"] = AnalysisContextItem(
            status=ContextFieldStatus.AVAILABLE,
            value={"warnings": guard_warnings},
            source=source,
        )

    return AnalysisContextBlock(
        status=status,
        items=items,
        source=source,
        warnings=guard_warnings,
        metadata=metadata,
    )


def _fundamental_guard_warnings(
    context: Mapping[str, Any],
    artifacts: PipelineAnalysisArtifacts,
) -> List[str]:
    """Derive low-sensitivity prompt guardrails from already-fetched inputs."""
    warnings: List[str] = []
    stock_flow = _capital_flow_stock_flow(context)
    flow_values = [
        _numeric_value(stock_flow.get("main_net_inflow")),
        _numeric_value(stock_flow.get("inflow_5d")),
        _numeric_value(stock_flow.get("inflow_10d")),
    ]
    present_flows = [value for value in flow_values if value is not None]

    if len(present_flows) >= 2 and all(value < 0 for value in present_flows):
        warnings.append(_CAPITAL_FLOW_BROKE_PROXY_WARNING)
    elif any(value < 0 for value in present_flows) and any(value > 0 for value in present_flows):
        warnings.append(_CAPITAL_FLOW_CONFLICT_WARNING)

    quote = _to_dict(artifacts.realtime_quote)
    trend = _to_dict(artifacts.trend_result)
    change_60d = _numeric_value(quote.get("change_60d"))
    bias_ma5 = _numeric_value(trend.get("bias_ma5"))
    volume_ratio = _numeric_value(quote.get("volume_ratio"))
    if volume_ratio is None:
        volume_ratio = _numeric_value(trend.get("volume_ratio_5d"))
    turnover_rate = _numeric_value(quote.get("turnover_rate"))
    has_confirmed_inflow = any(value is not None and value > 0 for value in present_flows)
    if (
        (
            (change_60d is not None and change_60d >= 25)
            or (bias_ma5 is not None and bias_ma5 > 5)
        )
        and (
            (volume_ratio is not None and volume_ratio >= 2.0)
            or (turnover_rate is not None and turnover_rate >= 5.0)
        )
        and not has_confirmed_inflow
    ):
        warnings.append(_PRICE_FLOW_HOT_WITHOUT_INFLOW_WARNING)

    return warnings


def _build_factor_snapshot_block(
    artifacts: PipelineAnalysisArtifacts,
    *,
    capital_flow_warnings: Sequence[str],
) -> AnalysisContextBlock:
    """Build a compact DuckDB-style factor snapshot from fetched artifacts only."""
    quote = _to_dict(artifacts.realtime_quote)
    trend = _to_dict(artifacts.trend_result)
    context = (
        artifacts.fundamental_context
        if isinstance(artifacts.fundamental_context, Mapping)
        else {}
    )
    coverage = context.get("coverage") if isinstance(context.get("coverage"), Mapping) else {}

    dimensions = [
        _technical_score_dimension(trend),
        _price_heat_dimension(trend, quote),
        _volume_price_dimension(trend, quote),
        _industry_theme_dimension(context, coverage),
        _coverage_dimension(
            "valuation",
            [coverage.get("valuation")],
            missing_reason="valuation_snapshot_missing",
            not_supported_reason="valuation_not_supported",
        ),
        _coverage_dimension(
            "quality_growth",
            [coverage.get("growth"), coverage.get("earnings")],
            missing_reason="quality_growth_snapshot_missing",
            not_supported_reason="quality_growth_not_supported",
        ),
        _fund_flow_dimension(context, coverage, capital_flow_warnings),
        _de_risk_dimension(context, coverage, capital_flow_warnings),
        _data_coverage_dimension(artifacts, quote, trend, context, coverage),
    ]
    dimensions.append(_risk_dimension(trend, dimensions, capital_flow_warnings))
    dimensions.append(_confidence_dimension(dimensions, capital_flow_warnings))

    available_count = sum(
        1 for dimension in dimensions if _dimension_is_core_available(dimension)
    )
    if available_count >= 5:
        block_status = ContextFieldStatus.AVAILABLE
    elif available_count > 0:
        block_status = ContextFieldStatus.PARTIAL
    else:
        block_status = ContextFieldStatus.MISSING

    warnings: List[str] = []
    _extend_unique(warnings, capital_flow_warnings)
    if any(
        dimension["name"] == "price_heat" and dimension.get("label") == "overheated"
        for dimension in dimensions
    ):
        warnings.append(_FACTOR_PRICE_OVERHEATED_WARNING)
    if dimensions[-1].get("label") == "low":
        warnings.append(_FACTOR_LOW_CONFIDENCE_WARNING)

    items: Dict[str, AnalysisContextItem] = {}
    for dimension in dimensions:
        value = {"label": dimension["label"]} if dimension.get("label") else None
        items[dimension["name"]] = AnalysisContextItem(
            status=dimension["status"],
            value=value,
            source=_FACTOR_SNAPSHOT_SOURCE,
            missing_reason=dimension.get("missing_reason"),
            warnings=list(dimension.get("warnings") or []),
        )

    metadata = {
        "dimensions": [
            {
                key: (
                    value.value
                    if isinstance(value, ContextFieldStatus)
                    else value
                )
                for key, value in {
                    "name": dimension["name"],
                    "status": dimension["status"],
                    "label": dimension.get("label"),
                    "missing_reason": dimension.get("missing_reason"),
                }.items()
                if value not in (None, "")
            }
            for dimension in dimensions
        ],
        "derived_from": _factor_snapshot_sources(quote, trend, context),
    }

    return AnalysisContextBlock(
        status=block_status,
        items=items,
        source=_FACTOR_SNAPSHOT_SOURCE,
        warnings=warnings,
        metadata=metadata,
    )


def _technical_score_dimension(trend: Mapping[str, Any]) -> Dict[str, Any]:
    score = _numeric_value(trend.get("signal_score"))
    if score is None:
        return _dimension(
            "technical_score",
            ContextFieldStatus.MISSING,
            missing_reason="technical_score_missing",
        )
    if score >= 75:
        label = "strong"
    elif score >= 60:
        label = "constructive"
    elif score >= 45:
        label = "neutral"
    else:
        label = "weak"
    return _dimension("technical_score", ContextFieldStatus.AVAILABLE, label=label)


def _price_heat_dimension(
    trend: Mapping[str, Any],
    quote: Mapping[str, Any],
) -> Dict[str, Any]:
    bias_ma5 = _numeric_value(trend.get("bias_ma5"))
    change_60d = _numeric_value(quote.get("change_60d"))
    if bias_ma5 is None and change_60d is None:
        return _dimension(
            "price_heat",
            ContextFieldStatus.MISSING,
            missing_reason="price_heat_inputs_missing",
        )
    if (bias_ma5 is not None and bias_ma5 > 5) or (
        change_60d is not None and change_60d >= 25
    ):
        label = "overheated"
    elif (bias_ma5 is not None and bias_ma5 > 3) or (
        change_60d is not None and change_60d >= 15
    ):
        label = "extended"
    elif (bias_ma5 is not None and bias_ma5 <= -5) or (
        change_60d is not None and change_60d <= -15
    ):
        label = "cooling"
    else:
        label = "normal"
    return _dimension("price_heat", ContextFieldStatus.AVAILABLE, label=label)


def _volume_price_dimension(
    trend: Mapping[str, Any],
    quote: Mapping[str, Any],
) -> Dict[str, Any]:
    volume_ratio = _numeric_value(quote.get("volume_ratio"))
    if volume_ratio is None:
        volume_ratio = _numeric_value(trend.get("volume_ratio_5d"))
    turnover_rate = _numeric_value(quote.get("turnover_rate"))
    if volume_ratio is None and turnover_rate is None:
        return _dimension(
            "volume_price",
            ContextFieldStatus.MISSING,
            missing_reason="volume_price_inputs_missing",
        )
    if (volume_ratio is not None and volume_ratio >= 2.0) or (
        turnover_rate is not None and turnover_rate >= 5.0
    ):
        label = "high_activity"
    elif (volume_ratio is not None and volume_ratio >= 1.2) or (
        turnover_rate is not None and turnover_rate >= 2.0
    ):
        label = "active"
    elif (volume_ratio is not None and volume_ratio <= 0.8) or (
        turnover_rate is not None and turnover_rate <= 0.5
    ):
        label = "quiet"
    else:
        label = "normal"
    return _dimension("volume_price", ContextFieldStatus.AVAILABLE, label=label)


def _industry_theme_dimension(
    context: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> Dict[str, Any]:
    belong_names = _board_names(context.get("belong_boards"))
    boards_block = context.get("boards") if isinstance(context, Mapping) else None
    boards_data = (
        boards_block.get("data")
        if isinstance(boards_block, Mapping) and isinstance(boards_block.get("data"), Mapping)
        else {}
    )
    top_names = _board_names(boards_data.get("top"))
    bottom_names = _board_names(boards_data.get("bottom"))
    board_status = _coverage_status(coverage.get("boards"))
    if not board_status and isinstance(boards_block, Mapping):
        board_status = _coverage_status(boards_block.get("status"))

    if belong_names & top_names:
        return _dimension("industry_theme", ContextFieldStatus.AVAILABLE, label="theme_tailwind")
    if belong_names & bottom_names:
        return _dimension("industry_theme", ContextFieldStatus.AVAILABLE, label="theme_headwind")
    if belong_names and (top_names or bottom_names):
        return _dimension("industry_theme", ContextFieldStatus.AVAILABLE, label="theme_neutral")
    if belong_names:
        return _dimension("industry_theme", ContextFieldStatus.AVAILABLE, label="membership_available")
    if top_names or bottom_names:
        return _dimension("industry_theme", ContextFieldStatus.PARTIAL, label="market_theme_available")
    if board_status == "not_supported":
        return _dimension(
            "industry_theme",
            ContextFieldStatus.NOT_SUPPORTED,
            label="not_supported",
            missing_reason="industry_theme_not_supported",
        )
    return _dimension(
        "industry_theme",
        ContextFieldStatus.MISSING,
        missing_reason="industry_theme_snapshot_missing",
    )


def _coverage_dimension(
    name: str,
    values: Sequence[Any],
    *,
    missing_reason: str,
    not_supported_reason: str,
) -> Dict[str, Any]:
    statuses = [_coverage_status(value) for value in values]
    statuses = [status for status in statuses if status]
    if not statuses:
        return _dimension(
            name,
            ContextFieldStatus.MISSING,
            missing_reason=missing_reason,
        )
    if any(status in {"ok", "available"} for status in statuses):
        return _dimension(name, ContextFieldStatus.AVAILABLE, label="available")
    if any(status == "partial" for status in statuses):
        return _dimension(name, ContextFieldStatus.PARTIAL, label="partial")
    if all(status == "not_supported" for status in statuses):
        return _dimension(
            name,
            ContextFieldStatus.NOT_SUPPORTED,
            label="not_supported",
            missing_reason=not_supported_reason,
        )
    return _dimension(name, ContextFieldStatus.MISSING, missing_reason=missing_reason)


def _fund_flow_dimension(
    context: Mapping[str, Any],
    coverage: Mapping[str, Any],
    capital_flow_warnings: Sequence[str],
) -> Dict[str, Any]:
    if capital_flow_warnings:
        return _dimension(
            "fund_flow",
            ContextFieldStatus.AVAILABLE,
            label="risk_guard",
            warnings=capital_flow_warnings,
        )
    coverage_status = _coverage_status(coverage.get("capital_flow"))
    block = context.get("capital_flow") if isinstance(context, Mapping) else None
    block_status = (
        _coverage_status(block.get("status"))
        if isinstance(block, Mapping)
        else None
    )
    status_value = coverage_status or block_status
    if status_value in {"ok", "available"}:
        return _dimension("fund_flow", ContextFieldStatus.AVAILABLE, label="available")
    if status_value == "partial":
        return _dimension("fund_flow", ContextFieldStatus.PARTIAL, label="partial")
    if status_value == "not_supported":
        return _dimension(
            "fund_flow",
            ContextFieldStatus.NOT_SUPPORTED,
            label="not_supported",
            missing_reason="fund_flow_not_supported",
        )
    return _dimension(
        "fund_flow",
        ContextFieldStatus.MISSING,
        missing_reason="fund_flow_snapshot_missing",
    )


def _de_risk_dimension(
    context: Mapping[str, Any],
    coverage: Mapping[str, Any],
    capital_flow_warnings: Sequence[str],
) -> Dict[str, Any]:
    warning_set = {warning for warning in capital_flow_warnings if warning}
    has_flow_broke = _CAPITAL_FLOW_BROKE_PROXY_WARNING in warning_set
    has_price_hot = _PRICE_FLOW_HOT_WITHOUT_INFLOW_WARNING in warning_set
    has_flow_conflict = _CAPITAL_FLOW_CONFLICT_WARNING in warning_set

    if has_flow_broke and has_price_hot:
        label = "flow_broke_price_flow_hot"
    elif has_flow_broke:
        label = "flow_broke"
    elif has_flow_conflict and has_price_hot:
        label = "flow_conflict_price_flow_hot"
    elif has_price_hot:
        label = "price_flow_hot"
    elif has_flow_conflict:
        label = "flow_conflict"
    elif _has_capital_flow_values(context):
        label = "clear"
    else:
        return _missing_de_risk_dimension(context, coverage)

    return _dimension(
        "de_risk",
        ContextFieldStatus.AVAILABLE,
        label=label,
        warnings=capital_flow_warnings,
    )


def _data_coverage_dimension(
    artifacts: PipelineAnalysisArtifacts,
    quote: Mapping[str, Any],
    trend: Mapping[str, Any],
    context: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> Dict[str, Any]:
    score = 0.0
    total = 0.0

    def add_check(is_available: bool, *, weight: float = 1.0) -> None:
        nonlocal score, total
        total += weight
        if is_available:
            score += weight

    add_check(bool(quote), weight=1.5)
    add_check(_has_daily_bar(artifacts.base_context, "today"))
    add_check(_has_daily_bar(artifacts.base_context, "yesterday"))
    add_check(bool(trend), weight=1.5)
    add_check(bool(context), weight=1.0)
    add_check((artifacts.news_result_count or 0) > 0, weight=1.0)

    for key in ("valuation", "growth", "earnings", "capital_flow", "boards"):
        status = _coverage_status(coverage.get(key))
        if status is None:
            continue
        total += 0.5
        if status in {"ok", "available"}:
            score += 0.5
        elif status == "partial":
            score += 0.25

    if total <= 0 or score <= 0:
        return _dimension(
            "data_coverage",
            ContextFieldStatus.MISSING,
            missing_reason="data_coverage_inputs_missing",
        )

    ratio = score / total
    if ratio >= 0.75:
        status = ContextFieldStatus.AVAILABLE
        label = "high"
    elif ratio >= 0.50:
        status = ContextFieldStatus.PARTIAL
        label = "medium"
    else:
        status = ContextFieldStatus.PARTIAL
        label = "low"
    return _dimension("data_coverage", status, label=label)


def _missing_de_risk_dimension(
    context: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> Dict[str, Any]:
    coverage_status = _coverage_status(coverage.get("capital_flow"))
    block = context.get("capital_flow") if isinstance(context, Mapping) else None
    block_status = (
        _coverage_status(block.get("status"))
        if isinstance(block, Mapping)
        else None
    )
    status_value = coverage_status or block_status
    if status_value == "not_supported":
        return _dimension(
            "de_risk",
            ContextFieldStatus.NOT_SUPPORTED,
            label="not_supported",
            missing_reason="de_risk_not_supported",
        )
    if status_value == "partial":
        return _dimension(
            "de_risk",
            ContextFieldStatus.PARTIAL,
            label="insufficient_flow",
            missing_reason="de_risk_signal_partial",
        )
    return _dimension(
        "de_risk",
        ContextFieldStatus.MISSING,
        missing_reason="de_risk_signal_missing",
    )


def _risk_dimension(
    trend: Mapping[str, Any],
    dimensions: Sequence[Mapping[str, Any]],
    capital_flow_warnings: Sequence[str],
) -> Dict[str, Any]:
    risk_factors = trend.get("risk_factors")
    trend_risk_count = len(risk_factors) if isinstance(risk_factors, list) else 0
    price_heat_risk = any(
        dimension.get("name") == "price_heat"
        and dimension.get("label") == "overheated"
        for dimension in dimensions
    )
    warning_count = len([warning for warning in capital_flow_warnings if warning])
    risk_count = trend_risk_count + warning_count + (1 if price_heat_risk else 0)
    if trend or warning_count or price_heat_risk:
        return _dimension(
            "risk",
            ContextFieldStatus.AVAILABLE,
            label="has_risk_flags" if risk_count else "no_risk_flags",
        )
    return _dimension("risk", ContextFieldStatus.MISSING, missing_reason="risk_inputs_missing")


def _confidence_dimension(
    dimensions: Sequence[Mapping[str, Any]],
    capital_flow_warnings: Sequence[str],
) -> Dict[str, Any]:
    available_count = sum(
        1 for dimension in dimensions if _dimension_is_core_available(dimension)
    )
    if available_count >= 5 and not capital_flow_warnings:
        label = "high"
    elif available_count >= 3:
        label = "medium"
    elif available_count > 0:
        label = "low"
    else:
        return _dimension(
            "confidence",
            ContextFieldStatus.MISSING,
            missing_reason="factor_snapshot_inputs_missing",
        )
    return _dimension("confidence", ContextFieldStatus.AVAILABLE, label=label)


def _board_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    names: set[str] = set()
    for item in value:
        item_map = item if isinstance(item, Mapping) else {}
        name = (
            item_map.get("name")
            or item_map.get("board_name")
            or item_map.get("板块名称")
            or item_map.get("industry")
        )
        name_text = str(name or "").strip().lower()
        if name_text:
            names.add(name_text)
    return names


def _dimension(
    name: str,
    status: ContextFieldStatus,
    *,
    label: Optional[str] = None,
    missing_reason: Optional[str] = None,
    warnings: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "label": label,
        "missing_reason": missing_reason,
        "warnings": [warning for warning in warnings or [] if warning],
    }


def _dimension_is_available(dimension: Mapping[str, Any]) -> bool:
    return dimension.get("status") in {
        ContextFieldStatus.AVAILABLE,
        ContextFieldStatus.PARTIAL,
        ContextFieldStatus.FALLBACK,
        ContextFieldStatus.STALE,
        ContextFieldStatus.ESTIMATED,
    }


def _dimension_is_core_available(dimension: Mapping[str, Any]) -> bool:
    if dimension.get("name") in {"data_coverage", "confidence"}:
        return False
    return _dimension_is_available(dimension)


def _coverage_status(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _factor_snapshot_sources(
    quote: Mapping[str, Any],
    trend: Mapping[str, Any],
    context: Mapping[str, Any],
) -> List[str]:
    sources: List[str] = []
    if quote:
        sources.append("quote")
    if trend:
        sources.append("technical")
    if context:
        sources.append("fundamentals")
    return sources


def _extend_unique(target: List[str], values: Sequence[str]) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)


def _capital_flow_stock_flow(context: Mapping[str, Any]) -> Dict[str, Any]:
    block = context.get("capital_flow")
    if not isinstance(block, Mapping):
        return {}
    data = block.get("data") if isinstance(block.get("data"), Mapping) else block
    stock_flow = data.get("stock_flow") if isinstance(data, Mapping) else None
    return dict(stock_flow) if isinstance(stock_flow, Mapping) else {}


def _has_capital_flow_values(context: Mapping[str, Any]) -> bool:
    stock_flow = _capital_flow_stock_flow(context)
    return any(
        _numeric_value(stock_flow.get(key)) is not None
        for key in ("main_net_inflow", "inflow_5d", "inflow_10d")
    )


def _has_daily_bar(context: Mapping[str, Any], key: str) -> bool:
    value = context.get(key) if isinstance(context, Mapping) else None
    return bool(value) if isinstance(value, Mapping) else value not in (None, "")


def _numeric_value(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _build_news_block(artifacts: PipelineAnalysisArtifacts) -> AnalysisContextBlock:
    content = (artifacts.news_context or "").strip()
    metadata: Dict[str, Any] = {}
    if artifacts.news_result_count is not None:
        metadata["news_result_count"] = artifacts.news_result_count

    if not content:
        return AnalysisContextBlock(
            status=ContextFieldStatus.MISSING,
            items={
                "content": AnalysisContextItem(
                    status=ContextFieldStatus.MISSING,
                    missing_reason="news_context_missing",
                )
            },
            metadata=metadata,
        )

    return AnalysisContextBlock(
        status=ContextFieldStatus.AVAILABLE,
        items={
            "content": AnalysisContextItem(
                status=ContextFieldStatus.AVAILABLE,
                value=content,
            )
        },
        metadata=metadata,
    )


def _to_dict(value: Optional[Any]) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if not isinstance(result, Mapping):
            raise TypeError(
                f"{type(value).__name__}.to_dict() must return a mapping"
            )
        return dict(result)
    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict):
        return dict(value_dict)
    return {}


def _source_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        value = enum_value
    text = str(value).strip()
    return text or None


def _metadata_value(metadata: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = (metadata or {}).get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _has_explicit_quote_stale_marker(
    artifacts: PipelineAnalysisArtifacts,
    quote: Dict[str, Any],
) -> bool:
    metadata = artifacts.metadata or {}
    for key in (
        "price_stale",
        "quote_stale",
        "quote_stale_seconds",
        "stale_seconds",
    ):
        if bool(metadata.get(key)) or bool(quote.get(key)):
            return True
    return False


def _quote_metadata(
    artifacts: PipelineAnalysisArtifacts,
    quote: Dict[str, Any],
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for key in (
        "price_stale",
        "quote_stale",
        "quote_stale_seconds",
        "stale_seconds",
    ):
        value = (artifacts.metadata or {}).get(key)
        if value is None:
            value = quote.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


def _has_realtime_overlay(enhanced_context: Dict[str, Any]) -> bool:
    today = (enhanced_context or {}).get("today")
    if not isinstance(today, dict):
        return False
    data_source = today.get("data_source") or today.get("dataSource")
    return isinstance(data_source, str) and data_source.startswith("realtime:")


def _realtime_overlay_source(enhanced_context: Dict[str, Any]) -> Optional[str]:
    today = (enhanced_context or {}).get("today")
    if not isinstance(today, dict):
        return None
    value = today.get("data_source") or today.get("dataSource")
    return value if isinstance(value, str) and value else None


def _fundamental_status(status: str) -> ContextFieldStatus:
    if status in {"ok", "available"}:
        return ContextFieldStatus.AVAILABLE
    if status == "not_supported":
        return ContextFieldStatus.NOT_SUPPORTED
    if status == "partial":
        return ContextFieldStatus.PARTIAL
    return ContextFieldStatus.MISSING


def _fundamental_payload_status(
    block_status: ContextFieldStatus,
    has_payload: bool,
) -> ContextFieldStatus:
    if has_payload:
        return block_status
    if block_status == ContextFieldStatus.NOT_SUPPORTED:
        return ContextFieldStatus.NOT_SUPPORTED
    return ContextFieldStatus.MISSING


def _fundamental_payload_missing_reason(
    raw_status: str,
    has_payload: bool,
    missing_reason: str,
) -> Optional[str]:
    if raw_status == "failed":
        return _FUNDAMENTAL_FAILED_REASON
    if raw_status == "not_supported":
        return "fundamentals_not_supported"
    if has_payload:
        return None
    return missing_reason


def _source_from_chain(source_chain: Any) -> Optional[str]:
    if not isinstance(source_chain, list) or not source_chain:
        return None
    first = source_chain[0]
    if isinstance(first, dict):
        return _source_text(first.get("provider") or first.get("source"))
    return _source_text(first)
