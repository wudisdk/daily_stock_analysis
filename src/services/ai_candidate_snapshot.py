# -*- coding: utf-8 -*-
"""Low-sensitivity AI candidate snapshot export for hosted daily runs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.services.analysis_context_builder import (
    AnalysisContextBuilder,
    PipelineAnalysisArtifacts,
)
from src.utils.sanitize import redact_sensitive_mapping


SNAPSHOT_SCHEMA_VERSION = "1.0"
SNAPSHOT_KIND = "post_analysis_candidate"
DEFAULT_SNAPSHOT_DIR = Path("reports") / "ai_snapshot"


def build_ai_candidate_snapshot_rows(
    results: Sequence[Any],
    *,
    created_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build deterministic, low-sensitivity JSONL rows from analysis results."""
    timestamp = _iso_timestamp(created_at)
    successful_results = [
        result
        for result in results or []
        if result is not None and bool(getattr(result, "success", True))
    ]
    ordered_results = sorted(
        successful_results,
        key=lambda item: (-_numeric_score(item), _stock_code(item)),
    )

    rows: List[Dict[str, Any]] = []
    for rank, result in enumerate(ordered_results, start=1):
        rows.append(
            redact_sensitive_mapping(
                _build_snapshot_row(
                    result,
                    rank=rank,
                    created_at=timestamp,
                    run_id=run_id,
                )
            )
        )
    return rows


def write_ai_candidate_snapshot_files(
    results: Sequence[Any],
    *,
    output_dir: Path | str = DEFAULT_SNAPSHOT_DIR,
    created_at: Optional[datetime] = None,
    run_id: Optional[str] = None,
) -> List[Path]:
    """Write latest and dated JSONL candidate snapshot files."""
    rows = build_ai_candidate_snapshot_rows(
        results,
        created_at=created_at,
        run_id=run_id,
    )
    if not rows:
        return []

    timestamp = _iso_timestamp(created_at)
    trade_date = _snapshot_file_date(rows, timestamp)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    latest_path = target_dir / "stock_ai_candidate_snapshot_latest.jsonl"
    dated_path = target_dir / f"stock_ai_candidate_snapshot_{trade_date}.jsonl"
    _write_jsonl(latest_path, rows)
    _write_jsonl(dated_path, rows)
    return [latest_path] if latest_path == dated_path else [latest_path, dated_path]


def _build_snapshot_row(
    result: Any,
    *,
    rank: int,
    created_at: str,
    run_id: Optional[str],
) -> Dict[str, Any]:
    snapshot = _mapping(getattr(result, "diagnostic_context_snapshot", None))
    enhanced_context = _mapping(snapshot.get("enhanced_context"))
    factor_snapshot, data_coverage = _derive_factor_snapshot(
        result,
        snapshot,
        enhanced_context,
    )
    model_result = {
        "sentiment_score": _numeric_or_none(getattr(result, "sentiment_score", None)),
        "decision_type": _safe_text(getattr(result, "decision_type", None)),
        "confidence_level": _safe_text(getattr(result, "confidence_level", None)),
        "operation_advice": _safe_text(getattr(result, "operation_advice", None)),
    }
    model_result = {
        key: value
        for key, value in model_result.items()
        if value not in (None, "")
    }

    row = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "snapshot_kind": SNAPSHOT_KIND,
        "created_at": created_at,
        "run_id": run_id,
        "query_id": _safe_text(getattr(result, "query_id", None)),
        "candidate_source": "daily_analysis",
        "candidate_rank": rank,
        "trade_date": _safe_text(enhanced_context.get("date")),
        "stock_code": _stock_code(result),
        "stock_name": _safe_text(getattr(result, "name", None)),
        "report_language": _safe_text(getattr(result, "report_language", None)),
        "model_used": _safe_text(getattr(result, "model_used", None)),
        "model_result": model_result,
        "factor_snapshot": factor_snapshot,
        "data_coverage": data_coverage,
        "news_result_count": _int_or_none(snapshot.get("news_result_count")),
    }
    return {
        key: value
        for key, value in row.items()
        if value not in (None, "", {}, [])
    }


def _derive_factor_snapshot(
    result: Any,
    snapshot: Mapping[str, Any],
    enhanced_context: Mapping[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    stock_code = _stock_code(result)
    stock_name = _safe_text(getattr(result, "name", None))
    realtime_quote = (
        _mapping(snapshot.get("realtime_quote_raw"))
        or _mapping(snapshot.get("realtime_quote"))
        or _mapping(enhanced_context.get("realtime"))
    )
    fundamental_context = (
        _mapping(getattr(result, "fundamental_context", None))
        or _mapping(snapshot.get("fundamental_context"))
        or _mapping(enhanced_context.get("fundamental_context"))
    )
    trend_result = (
        _mapping(enhanced_context.get("trend_analysis"))
        or _mapping(snapshot.get("trend_result"))
    )
    artifacts = PipelineAnalysisArtifacts(
        code=stock_code,
        stock_name=stock_name,
        market=_safe_text(enhanced_context.get("market")) or "",
        phase=None,
        base_context=dict(enhanced_context),
        enhanced_context=dict(enhanced_context),
        realtime_quote=dict(realtime_quote) if realtime_quote else None,
        trend_result=dict(trend_result),
        chip_data=dict(_mapping(snapshot.get("chip_distribution_raw"))) or None,
        fundamental_context=dict(fundamental_context) if fundamental_context else None,
        news_context=None,
        news_result_count=_int_or_none(snapshot.get("news_result_count")),
        metadata={
            "query_id": _safe_text(getattr(result, "query_id", None)),
            "trigger_source": "ai_candidate_snapshot",
        },
    )
    pack = AnalysisContextBuilder.build(artifacts).to_safe_dict()
    blocks = _mapping(pack.get("blocks"))
    factor_block = _mapping(blocks.get("factor_snapshot"))
    metadata = _mapping(factor_block.get("metadata"))
    factor_snapshot = {
        "status": _safe_text(factor_block.get("status")),
        "warnings": _safe_string_list(factor_block.get("warnings")),
        "dimensions": _safe_dimensions(metadata.get("dimensions")),
        "derived_from": _safe_string_list(metadata.get("derived_from")),
    }
    data_coverage = {
        key: _block_quality_summary(blocks.get(key))
        for key in (
            "quote",
            "daily_bars",
            "technical",
            "fundamentals",
            "factor_snapshot",
            "news",
        )
    }
    return _drop_empty(factor_snapshot), _drop_empty(data_coverage)


def _block_quality_summary(block: Any) -> Dict[str, Any]:
    block_map = _mapping(block)
    if not block_map:
        return {}
    summary = {
        "status": _safe_text(block_map.get("status")),
        "warnings": _safe_string_list(block_map.get("warnings")),
        "missing_reasons": _item_missing_reasons(block_map.get("items")),
    }
    return _drop_empty(summary)


def _item_missing_reasons(items: Any) -> List[str]:
    items_map = _mapping(items)
    reasons: List[str] = []
    for item in items_map.values():
        item_map = _mapping(item)
        reason = _safe_text(item_map.get("missing_reason"))
        if reason and reason not in reasons:
            reasons.append(reason)
    return reasons[:5]


def _safe_dimensions(value: Any) -> List[Dict[str, Any]]:
    dimensions = value if isinstance(value, list) else []
    safe: List[Dict[str, Any]] = []
    for dimension in dimensions:
        dimension_map = _mapping(dimension)
        safe_dimension = {
            key: _safe_text(dimension_map.get(key))
            for key in ("name", "status", "label", "missing_reason")
        }
        safe_dimension = _drop_empty(safe_dimension)
        if safe_dimension:
            safe.append(safe_dimension)
    return safe


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            file.write("\n")


def _snapshot_file_date(rows: Sequence[Mapping[str, Any]], timestamp: str) -> str:
    for row in rows:
        trade_date = _safe_text(row.get("trade_date"))
        if trade_date:
            return trade_date.replace("-", "")
    return timestamp[:10].replace("-", "")


def _numeric_score(result: Any) -> int:
    value = _numeric_or_none(getattr(result, "sentiment_score", None))
    return int(value) if value is not None else -1


def _numeric_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace("%", "").replace(",", ""))
        except ValueError:
            return None
    return None


def _int_or_none(value: Any) -> Optional[int]:
    numeric = _numeric_or_none(value)
    return int(numeric) if numeric is not None else None


def _stock_code(result: Any) -> str:
    return _safe_text(getattr(result, "code", None))


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        text = _safe_text(item)
        if text and text not in result:
            result.append(text)
    return result[:10]


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _drop_empty(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", {}, [])
    }


def _iso_timestamp(value: Optional[datetime]) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()
