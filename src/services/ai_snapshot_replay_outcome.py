# -*- coding: utf-8 -*-
"""Resolve replay queue rows against point-in-time price history."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional


OUTCOME_SCHEMA_VERSION = "1.3"
OUTCOME_AUDIT_SCHEMA_VERSION = "1.0"
GUARD_POLICY_MIN_PAIRED_SAMPLES = 5
GUARD_POLICY_MATERIAL_EXCESS_DELTA = 0.01
GUARD_POLICY_ASSUMED_ROUND_TRIP_COST_BPS = 30
DEFAULT_QUEUE_PATH = Path("reports") / "ai_snapshot" / "stock_ai_candidate_replay_queue_latest.jsonl"
DEFAULT_OUTPUT_DIR = Path("reports") / "ai_snapshot"
REQUIRED_QUEUE_FIELDS = {
    "outcome_join_key",
    "input_snapshot_hash",
    "stock_code",
    "trade_date",
    "eval_horizon",
    "forward_trading_days",
}
REQUIRED_PRICE_FIELDS = {"stock_code", "trade_date", "close"}
REQUIRED_OUTCOME_COLUMNS = REQUIRED_QUEUE_FIELDS | {
    "outcome_schema_version",
    "generated_at",
    "model_used",
    "outcome_status",
}
KNOWN_OUTCOME_STATUSES = {
    "available",
    "invalid_queue_row",
    "missing_anchor_price",
    "missing_future_price",
    "pending_future_price",
}
PRICE_JOIN_ERROR_STATUSES = {
    "invalid_queue_row",
    "missing_anchor_price",
    "missing_future_price",
}
FORBIDDEN_RAW_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
)
DIMENSION_NAMES = (
    "technical_score",
    "price_heat",
    "volume_price",
    "industry_theme",
    "valuation",
    "quality_growth",
    "fund_flow",
    "de_risk",
    "data_coverage",
    "risk",
    "confidence",
)


def build_replay_outcome_rows(
    queue_rows: Sequence[Mapping[str, Any]],
    price_rows: Sequence[Mapping[str, Any]],
    *,
    generated_at: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Join replay queue rows to future prices and equal-weight benchmark returns."""
    timestamp = _iso_timestamp(generated_at)
    price_index = _build_price_index(price_rows)
    benchmark_cache: Dict[tuple[str, int], Optional[float]] = {}
    outcomes: List[Dict[str, Any]] = []

    for row in queue_rows:
        base = _base_outcome_row(row, timestamp)
        stock_code = _safe_text(row.get("stock_code"))
        trade_date = _safe_text(row.get("trade_date"))
        forward_days = _int_or_none(row.get("forward_trading_days"))
        if not stock_code or not trade_date or forward_days is None:
            outcomes.append({**base, "outcome_status": "invalid_queue_row"})
            continue

        stock_prices = _lookup_stock_prices(price_index, stock_code)
        anchor_idx = _find_trade_date_index(stock_prices, trade_date)
        if anchor_idx is None:
            outcomes.append({**base, "outcome_status": "missing_anchor_price"})
            continue

        future_idx = anchor_idx + forward_days
        anchor = stock_prices[anchor_idx]
        if future_idx >= len(stock_prices):
            outcomes.append(
                {
                    **base,
                    "anchor_trade_date": anchor["trade_date"],
                    "anchor_close": anchor["close"],
                    "outcome_status": "pending_future_price",
                }
            )
            continue

        future = stock_prices[future_idx]
        future_return = _return(anchor["close"], future["close"])
        benchmark_key = (trade_date, forward_days)
        if benchmark_key not in benchmark_cache:
            benchmark_cache[benchmark_key] = _benchmark_return(price_index, trade_date, forward_days)
        benchmark_return = benchmark_cache[benchmark_key]
        outcome_status = "available" if future_return is not None else "missing_future_price"
        outcome = {
            **base,
            "anchor_trade_date": anchor["trade_date"],
            "future_trade_date": future["trade_date"],
            "anchor_close": anchor["close"],
            "future_close": future["close"],
            "future_return": future_return,
            "benchmark_return": benchmark_return,
            "excess_return": (
                round(future_return - benchmark_return, 10)
                if future_return is not None and benchmark_return is not None
                else None
            ),
            "outcome_status": outcome_status,
        }
        outcomes.append(_drop_none(outcome))
    return outcomes


def summarize_replay_outcomes(outcome_rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Summarize replay outcomes by horizon, model, and status."""
    groups: Dict[tuple[str, str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in outcome_rows:
        groups[
            (
                _safe_text(row.get("eval_horizon")),
                _safe_text(row.get("model_used")),
                _safe_text(row.get("outcome_status")),
            )
        ].append(row)

    summary: List[Dict[str, Any]] = []
    for (horizon, model, status), rows in sorted(groups.items()):
        summary.append(_summary_row(rows, eval_horizon=horizon, model_used=model, outcome_status=status))
    return summary


def summarize_replay_invalidation_outcomes(
    outcome_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Summarize outcomes for each invalidation flag vs rows without that flag."""
    observed_flags = sorted(
        {
            flag
            for row in outcome_rows
            for flag in _row_invalidation_flags(row)
        }
    )
    if not observed_flags:
        return []

    groups: Dict[tuple[str, str, str, str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in outcome_rows:
        row_flags = set(_row_invalidation_flags(row))
        for flag in observed_flags:
            state = "hit" if flag in row_flags else "miss"
            groups[
                (
                    _safe_text(row.get("eval_horizon")),
                    _safe_text(row.get("model_used")),
                    _safe_text(row.get("outcome_status")),
                    flag,
                    state,
                )
            ].append(row)

    summary: List[Dict[str, Any]] = []
    for (horizon, model, status, flag, state), rows in sorted(groups.items()):
        summary.append(
            _summary_row(
                rows,
                eval_horizon=horizon,
                model_used=model,
                outcome_status=status,
                invalidation_flag=flag,
                flag_state=state,
            )
        )
    return summary


def summarize_replay_invalidation_effects(
    outcome_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Compare each invalidation flag's hit group directly against its miss group."""
    invalidation_summary = summarize_replay_invalidation_outcomes(outcome_rows)
    groups: Dict[tuple[str, str, str, str], Dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in invalidation_summary:
        state = _safe_text(row.get("flag_state"))
        if state not in {"hit", "miss"}:
            continue
        groups[
            (
                _safe_text(row.get("eval_horizon")),
                _safe_text(row.get("model_used")),
                _safe_text(row.get("outcome_status")),
                _safe_text(row.get("invalidation_flag")),
            )
        ][state] = row

    effects: List[Dict[str, Any]] = []
    for (horizon, model, status, flag), by_state in sorted(groups.items()):
        hit = by_state.get("hit", {})
        miss = by_state.get("miss", {})
        hit_available = _int_or_none(hit.get("available_count")) or 0
        miss_available = _int_or_none(miss.get("available_count")) or 0
        effects.append(
            _drop_none(
                {
                    "eval_horizon": horizon,
                    "model_used": model,
                    "outcome_status": status,
                    "invalidation_flag": flag,
                    "hit_n_rows": _int_or_none(hit.get("n_rows")) or 0,
                    "miss_n_rows": _int_or_none(miss.get("n_rows")) or 0,
                    "hit_available_count": hit_available,
                    "miss_available_count": miss_available,
                    "hit_avg_future_return": _numeric_or_none(hit.get("avg_future_return")),
                    "miss_avg_future_return": _numeric_or_none(miss.get("avg_future_return")),
                    "delta_avg_future_return": _delta(
                        hit.get("avg_future_return"),
                        miss.get("avg_future_return"),
                    ),
                    "hit_avg_excess_return": _numeric_or_none(hit.get("avg_excess_return")),
                    "miss_avg_excess_return": _numeric_or_none(miss.get("avg_excess_return")),
                    "delta_avg_excess_return": _delta(
                        hit.get("avg_excess_return"),
                        miss.get("avg_excess_return"),
                    ),
                    "hit_excess_win_rate": _numeric_or_none(hit.get("excess_win_rate")),
                    "miss_excess_win_rate": _numeric_or_none(miss.get("excess_win_rate")),
                    "delta_excess_win_rate": _delta(hit.get("excess_win_rate"), miss.get("excess_win_rate")),
                    "effect_direction": _effect_direction(
                        _delta(hit.get("avg_excess_return"), miss.get("avg_excess_return"))
                    ),
                    "evidence_status": (
                        "paired_available" if hit_available and miss_available else "insufficient_pair"
                    ),
                }
            )
        )
    return effects


def summarize_replay_guard_policy_candidates(
    outcome_rows: Sequence[Mapping[str, Any]],
    *,
    min_paired_samples: int = GUARD_POLICY_MIN_PAIRED_SAMPLES,
    material_excess_delta: float = GUARD_POLICY_MATERIAL_EXCESS_DELTA,
    assumed_round_trip_cost_bps: float = GUARD_POLICY_ASSUMED_ROUND_TRIP_COST_BPS,
) -> List[Dict[str, Any]]:
    """Convert invalidation effect rows into dynamic exit/deweight candidates."""
    rows: List[Dict[str, Any]] = []
    cost_rate = _bps_to_rate(assumed_round_trip_cost_bps)
    for effect in summarize_replay_invalidation_effects(outcome_rows):
        if _safe_text(effect.get("outcome_status")) != "available":
            continue
        hit_count = _int_or_none(effect.get("hit_available_count")) or 0
        miss_count = _int_or_none(effect.get("miss_available_count")) or 0
        delta_excess = _numeric_or_none(effect.get("delta_avg_excess_return"))
        delta_win_rate = _numeric_or_none(effect.get("delta_excess_win_rate"))
        gross_deweight_edge = _guard_gross_deweight_edge(delta_excess)
        net_deweight_edge = _guard_net_deweight_edge(delta_excess, cost_rate)
        policy_signal = _guard_policy_signal(
            hit_count,
            miss_count,
            delta_excess,
            net_deweight_edge,
            min_paired_samples=min_paired_samples,
            material_excess_delta=material_excess_delta,
        )
        rows.append(
            _drop_none(
                {
                    "eval_horizon": _safe_text(effect.get("eval_horizon")),
                    "model_used": _safe_text(effect.get("model_used")),
                    "invalidation_flag": _safe_text(effect.get("invalidation_flag")),
                    "hit_available_count": hit_count,
                    "miss_available_count": miss_count,
                    "min_paired_samples": min_paired_samples,
                    "material_excess_delta": material_excess_delta,
                    "assumed_round_trip_cost_bps": assumed_round_trip_cost_bps,
                    "assumed_round_trip_cost_rate": cost_rate,
                    "hit_avg_excess_return": _numeric_or_none(effect.get("hit_avg_excess_return")),
                    "miss_avg_excess_return": _numeric_or_none(effect.get("miss_avg_excess_return")),
                    "delta_avg_excess_return": delta_excess,
                    "delta_excess_win_rate": delta_win_rate,
                    "gross_deweight_edge": gross_deweight_edge,
                    "net_deweight_edge_after_cost": net_deweight_edge,
                    "cost_buffer_status": _guard_cost_buffer_status(net_deweight_edge, material_excess_delta),
                    "effect_direction": _safe_text(effect.get("effect_direction")),
                    "directional_hint": _guard_directional_hint(delta_excess),
                    "evidence_strength": _guard_evidence_strength(hit_count, miss_count, min_paired_samples),
                    "policy_signal": policy_signal,
                    "recommended_action": _guard_recommended_action(policy_signal),
                    "rationale": _guard_policy_rationale(policy_signal),
                }
            )
        )
    return rows


def summarize_replay_dimension_outcomes(
    outcome_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Summarize future outcomes by low-sensitivity snapshot dimension labels."""
    groups: Dict[tuple[str, str, str, str, str, str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in outcome_rows:
        for dimension in DIMENSION_NAMES:
            status = _safe_text(row.get(f"{dimension}_status"))
            label = _safe_text(row.get(f"{dimension}_label"))
            missing_reason = _safe_text(row.get(f"{dimension}_missing_reason"))
            if not (status or label or missing_reason):
                continue
            groups[
                (
                    _safe_text(row.get("eval_horizon")),
                    _safe_text(row.get("model_used")),
                    _safe_text(row.get("outcome_status")),
                    dimension,
                    status,
                    label,
                    missing_reason,
                )
            ].append(row)

    summary: List[Dict[str, Any]] = []
    for (horizon, model, status, dimension, dimension_status, label, missing_reason), rows in sorted(groups.items()):
        summary.append(
            _summary_row(
                rows,
                eval_horizon=horizon,
                model_used=model,
                outcome_status=status,
                dimension_name=dimension,
                dimension_status=dimension_status,
                dimension_label=label,
                dimension_missing_reason=missing_reason,
            )
        )
    return summary


def summarize_replay_case_tag_outcomes(
    outcome_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Summarize future outcomes by low-sensitivity bull/bear replay tags."""
    groups: Dict[tuple[str, str, str, str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in outcome_rows:
        for tag in _row_case_tags(row):
            groups[
                (
                    _safe_text(row.get("eval_horizon")),
                    _safe_text(row.get("model_used")),
                    _safe_text(row.get("outcome_status")),
                    _case_tag_group(tag),
                    tag,
                )
            ].append(row)

    summary: List[Dict[str, Any]] = []
    for (horizon, model, status, tag_group, tag), rows in sorted(groups.items()):
        summary.append(
            _summary_row(
                rows,
                eval_horizon=horizon,
                model_used=model,
                outcome_status=status,
                case_tag_group=tag_group,
                case_tag=tag,
            )
        )
    return summary


def summarize_replay_model_outcomes(
    outcome_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Summarize future outcomes by model decision and sentiment-score bucket."""
    groups: Dict[tuple[str, str, str, str, str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in outcome_rows:
        groups[
            (
                _safe_text(row.get("eval_horizon")),
                _safe_text(row.get("model_used")),
                _safe_text(row.get("outcome_status")),
                _safe_text(row.get("model_decision_type")) or "missing_decision",
                _sentiment_bucket(row.get("model_sentiment_score")),
                _safe_text(row.get("model_confidence_level")) or "missing_confidence",
            )
        ].append(row)

    summary: List[Dict[str, Any]] = []
    for (horizon, model, status, decision_type, sentiment_bucket, confidence_level), rows in sorted(groups.items()):
        summary.append(
            _summary_row(
                rows,
                eval_horizon=horizon,
                model_used=model,
                outcome_status=status,
                model_decision_type=decision_type,
                model_sentiment_bucket=sentiment_bucket,
                model_confidence_level=confidence_level,
            )
        )
    return summary


def audit_replay_outcomes(
    outcome_rows: Sequence[Mapping[str, Any]],
    *,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Audit replay outcome rows and their validation summaries."""
    checks: List[Dict[str, Any]] = []
    _add_check(
        checks,
        "replay_outcome_rows_non_empty",
        "PASS" if outcome_rows else "FAIL",
        f"replay outcome row count={len(outcome_rows)}",
        {"row_count": len(outcome_rows)},
    )
    _audit_outcome_required_columns(checks, outcome_rows)
    _audit_outcome_statuses(checks, outcome_rows)
    _audit_future_price_availability(checks, outcome_rows)
    _audit_summary_visibility(checks, outcome_rows)
    _audit_low_sensitivity(checks, outcome_rows)

    status_counts = dict(Counter(check["status"] for check in checks))
    overall_status = "FAIL" if status_counts.get("FAIL") else ("WARN" if status_counts.get("WARN") else "PASS")
    trade_dates = sorted({_safe_text(row.get("trade_date")) for row in outcome_rows if _safe_text(row.get("trade_date"))})
    outcome_status_counts = dict(Counter(_safe_text(row.get("outcome_status")) for row in outcome_rows))
    return {
        "audit_schema_version": OUTCOME_AUDIT_SCHEMA_VERSION,
        "generated_at": _iso_timestamp(generated_at),
        "overall_status": overall_status,
        "status_counts": status_counts,
        "row_count": len(outcome_rows),
        "trade_dates": trade_dates,
        "latest_trade_date": max(trade_dates) if trade_dates else None,
        "outcome_status_counts": outcome_status_counts,
        "checks": checks,
    }


def write_replay_outcome_outputs(
    queue_path: Path | str,
    price_path: Path | str,
    *,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    generated_at: Optional[datetime] = None,
) -> List[Path]:
    """Write latest and dated outcome JSONL/CSV plus summary JSON/CSV."""
    queue_rows = _load_jsonl(Path(queue_path))
    price_rows = _load_price_rows(Path(price_path))
    outcomes = build_replay_outcome_rows(queue_rows, price_rows, generated_at=generated_at)
    if not outcomes:
        return []

    summary = summarize_replay_outcomes(outcomes)
    invalidation_summary = summarize_replay_invalidation_outcomes(outcomes)
    invalidation_effects = summarize_replay_invalidation_effects(outcomes)
    guard_policy = summarize_replay_guard_policy_candidates(outcomes)
    dimension_summary = summarize_replay_dimension_outcomes(outcomes)
    case_tag_summary = summarize_replay_case_tag_outcomes(outcomes)
    model_summary = summarize_replay_model_outcomes(outcomes)
    audit = audit_replay_outcomes(outcomes, generated_at=generated_at)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    outcome_date = _outcome_file_date(outcomes, generated_at)

    latest_jsonl = target_dir / "stock_ai_candidate_replay_outcome_latest.jsonl"
    dated_jsonl = target_dir / f"stock_ai_candidate_replay_outcome_{outcome_date}.jsonl"
    latest_csv = target_dir / "stock_ai_candidate_replay_outcome_latest.csv"
    dated_csv = target_dir / f"stock_ai_candidate_replay_outcome_{outcome_date}.csv"
    latest_summary_json = target_dir / "stock_ai_candidate_replay_outcome_summary_latest.json"
    dated_summary_json = target_dir / f"stock_ai_candidate_replay_outcome_summary_{outcome_date}.json"
    latest_summary_csv = target_dir / "stock_ai_candidate_replay_outcome_summary_latest.csv"
    dated_summary_csv = target_dir / f"stock_ai_candidate_replay_outcome_summary_{outcome_date}.csv"
    latest_invalidation_json = target_dir / "stock_ai_candidate_replay_invalidation_summary_latest.json"
    dated_invalidation_json = target_dir / f"stock_ai_candidate_replay_invalidation_summary_{outcome_date}.json"
    latest_invalidation_csv = target_dir / "stock_ai_candidate_replay_invalidation_summary_latest.csv"
    dated_invalidation_csv = target_dir / f"stock_ai_candidate_replay_invalidation_summary_{outcome_date}.csv"
    latest_effect_json = target_dir / "stock_ai_candidate_replay_invalidation_effect_latest.json"
    dated_effect_json = target_dir / f"stock_ai_candidate_replay_invalidation_effect_{outcome_date}.json"
    latest_effect_csv = target_dir / "stock_ai_candidate_replay_invalidation_effect_latest.csv"
    dated_effect_csv = target_dir / f"stock_ai_candidate_replay_invalidation_effect_{outcome_date}.csv"
    latest_guard_policy_json = target_dir / "stock_ai_candidate_replay_guard_policy_latest.json"
    dated_guard_policy_json = target_dir / f"stock_ai_candidate_replay_guard_policy_{outcome_date}.json"
    latest_guard_policy_csv = target_dir / "stock_ai_candidate_replay_guard_policy_latest.csv"
    dated_guard_policy_csv = target_dir / f"stock_ai_candidate_replay_guard_policy_{outcome_date}.csv"
    latest_dimension_json = target_dir / "stock_ai_candidate_replay_dimension_summary_latest.json"
    dated_dimension_json = target_dir / f"stock_ai_candidate_replay_dimension_summary_{outcome_date}.json"
    latest_dimension_csv = target_dir / "stock_ai_candidate_replay_dimension_summary_latest.csv"
    dated_dimension_csv = target_dir / f"stock_ai_candidate_replay_dimension_summary_{outcome_date}.csv"
    latest_case_tag_json = target_dir / "stock_ai_candidate_replay_case_tag_summary_latest.json"
    dated_case_tag_json = target_dir / f"stock_ai_candidate_replay_case_tag_summary_{outcome_date}.json"
    latest_case_tag_csv = target_dir / "stock_ai_candidate_replay_case_tag_summary_latest.csv"
    dated_case_tag_csv = target_dir / f"stock_ai_candidate_replay_case_tag_summary_{outcome_date}.csv"
    latest_model_json = target_dir / "stock_ai_candidate_replay_model_summary_latest.json"
    dated_model_json = target_dir / f"stock_ai_candidate_replay_model_summary_{outcome_date}.json"
    latest_model_csv = target_dir / "stock_ai_candidate_replay_model_summary_latest.csv"
    dated_model_csv = target_dir / f"stock_ai_candidate_replay_model_summary_{outcome_date}.csv"
    latest_audit_json = target_dir / "stock_ai_candidate_replay_outcome_audit_latest.json"
    dated_audit_json = target_dir / f"stock_ai_candidate_replay_outcome_audit_{outcome_date}.json"
    latest_audit_csv = target_dir / "stock_ai_candidate_replay_outcome_audit_latest.csv"
    dated_audit_csv = target_dir / f"stock_ai_candidate_replay_outcome_audit_{outcome_date}.csv"

    _write_jsonl(latest_jsonl, outcomes)
    _write_jsonl(dated_jsonl, outcomes)
    _write_csv(latest_csv, outcomes)
    _write_csv(dated_csv, outcomes)
    _write_json(latest_summary_json, summary)
    _write_json(dated_summary_json, summary)
    _write_csv(latest_summary_csv, summary)
    _write_csv(dated_summary_csv, summary)
    _write_json(latest_invalidation_json, invalidation_summary)
    _write_json(dated_invalidation_json, invalidation_summary)
    _write_csv(latest_invalidation_csv, invalidation_summary)
    _write_csv(dated_invalidation_csv, invalidation_summary)
    _write_json(latest_effect_json, invalidation_effects)
    _write_json(dated_effect_json, invalidation_effects)
    _write_csv(latest_effect_csv, invalidation_effects)
    _write_csv(dated_effect_csv, invalidation_effects)
    _write_json(latest_guard_policy_json, guard_policy)
    _write_json(dated_guard_policy_json, guard_policy)
    _write_csv(latest_guard_policy_csv, guard_policy)
    _write_csv(dated_guard_policy_csv, guard_policy)
    _write_json(latest_dimension_json, dimension_summary)
    _write_json(dated_dimension_json, dimension_summary)
    _write_csv(latest_dimension_csv, dimension_summary)
    _write_csv(dated_dimension_csv, dimension_summary)
    _write_json(latest_case_tag_json, case_tag_summary)
    _write_json(dated_case_tag_json, case_tag_summary)
    _write_csv(latest_case_tag_csv, case_tag_summary)
    _write_csv(dated_case_tag_csv, case_tag_summary)
    _write_json(latest_model_json, model_summary)
    _write_json(dated_model_json, model_summary)
    _write_csv(latest_model_csv, model_summary)
    _write_csv(dated_model_csv, model_summary)
    _write_json_value(latest_audit_json, audit)
    _write_json_value(dated_audit_json, audit)
    _write_csv(latest_audit_csv, audit.get("checks", []))
    _write_csv(dated_audit_csv, audit.get("checks", []))
    return [
        latest_jsonl,
        dated_jsonl,
        latest_csv,
        dated_csv,
        latest_summary_json,
        dated_summary_json,
        latest_summary_csv,
        dated_summary_csv,
        latest_invalidation_json,
        dated_invalidation_json,
        latest_invalidation_csv,
        dated_invalidation_csv,
        latest_effect_json,
        dated_effect_json,
        latest_effect_csv,
        dated_effect_csv,
        latest_guard_policy_json,
        dated_guard_policy_json,
        latest_guard_policy_csv,
        dated_guard_policy_csv,
        latest_dimension_json,
        dated_dimension_json,
        latest_dimension_csv,
        dated_dimension_csv,
        latest_case_tag_json,
        dated_case_tag_json,
        latest_case_tag_csv,
        dated_case_tag_csv,
        latest_model_json,
        dated_model_json,
        latest_model_csv,
        dated_model_csv,
        latest_audit_json,
        dated_audit_json,
        latest_audit_csv,
        dated_audit_csv,
    ]


def _audit_outcome_required_columns(
    checks: List[Dict[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    missing: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        missing_columns = sorted(column for column in REQUIRED_OUTCOME_COLUMNS if row.get(column) in (None, "", [], {}))
        if missing_columns:
            missing.append({"row": _row_identity(row, idx), "missing": missing_columns})
    _add_check(
        checks,
        "replay_outcome_required_columns",
        "FAIL" if missing else "PASS",
        "required replay outcome columns are missing" if missing else "all required replay outcome columns are present",
        {"required_columns": sorted(REQUIRED_OUTCOME_COLUMNS), "missing": missing[:20]},
    )


def _audit_outcome_statuses(
    checks: List[Dict[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    statuses = Counter(_safe_text(row.get("outcome_status")) for row in rows)
    unknown = sorted(status for status in statuses if status and status not in KNOWN_OUTCOME_STATUSES)
    missing_rows = [_row_identity(row, idx) for idx, row in enumerate(rows, start=1) if not _safe_text(row.get("outcome_status"))]
    _add_check(
        checks,
        "replay_outcome_statuses",
        "FAIL" if unknown or missing_rows else "PASS",
        "unknown or missing replay outcome statuses found"
        if unknown or missing_rows
        else "all replay outcome statuses are known",
        {"status_counts": dict(statuses), "unknown_statuses": unknown, "missing_rows": missing_rows[:20]},
    )


def _audit_future_price_availability(
    checks: List[Dict[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    statuses = Counter(_safe_text(row.get("outcome_status")) for row in rows)
    available_count = statuses.get("available", 0)
    pending_count = statuses.get("pending_future_price", 0)
    join_errors = {status: count for status, count in statuses.items() if status in PRICE_JOIN_ERROR_STATUSES and count}

    if join_errors:
        status = "FAIL"
        message = "price join errors are present in replay outcomes"
    elif available_count:
        status = "PASS"
        message = "at least one replay outcome has an available future price"
    elif pending_count:
        status = "WARN"
        message = "replay outcomes are waiting for future prices"
    else:
        status = "FAIL"
        message = "no available or pending future-price outcomes found"
    _add_check(
        checks,
        "replay_outcome_future_price_availability",
        status,
        message,
        {"available_count": available_count, "pending_count": pending_count, "join_errors": join_errors},
    )


def _audit_summary_visibility(
    checks: List[Dict[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    outcome_summary = summarize_replay_outcomes(rows)
    invalidation_summary = summarize_replay_invalidation_outcomes(rows)
    invalidation_effects = summarize_replay_invalidation_effects(rows)
    guard_policy = summarize_replay_guard_policy_candidates(rows)
    dimension_summary = summarize_replay_dimension_outcomes(rows)
    case_tag_summary = summarize_replay_case_tag_outcomes(rows)
    model_summary = summarize_replay_model_outcomes(rows)
    no_rows = not rows

    _add_check(
        checks,
        "replay_outcome_summary_visible",
        "FAIL" if no_rows or not outcome_summary else "PASS",
        "replay outcome summary is empty" if no_rows or not outcome_summary else "replay outcome summary is available",
        {"summary_rows": len(outcome_summary)},
    )
    _add_check(
        checks,
        "replay_model_summary_visible",
        "WARN" if rows and not model_summary else ("FAIL" if no_rows else "PASS"),
        "model summary is empty" if not model_summary else "model summary is available",
        {"summary_rows": len(model_summary)},
    )
    _add_check(
        checks,
        "replay_dimension_summary_visible",
        "WARN" if rows and not dimension_summary else ("FAIL" if no_rows else "PASS"),
        "dimension summary is empty" if not dimension_summary else "dimension summary is available",
        {"summary_rows": len(dimension_summary)},
    )
    _add_check(
        checks,
        "replay_case_tag_summary_visible",
        "WARN" if rows and not case_tag_summary else ("FAIL" if no_rows else "PASS"),
        "case tag summary is empty" if not case_tag_summary else "case tag summary is available",
        {"summary_rows": len(case_tag_summary)},
    )
    _add_check(
        checks,
        "replay_invalidation_summary_visible",
        "PASS",
        "invalidation summary is available" if invalidation_summary else "no invalidation flags observed",
        {
            "summary_rows": len(invalidation_summary),
            "effect_rows": len(invalidation_effects),
            "guard_policy_rows": len(guard_policy),
        },
    )
    _add_check(
        checks,
        "replay_guard_policy_visible",
        "PASS",
        "guard policy candidates are available" if guard_policy else "no available guard policy candidate pairs",
        {"summary_rows": len(guard_policy)},
    )


def _audit_low_sensitivity(
    checks: List[Dict[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    key_hits: List[Dict[str, Any]] = []
    for row_idx, row in enumerate(rows, start=1):
        for key_path, key in _walk_keys(row):
            normalized = _normalize_key(key)
            if normalized in FORBIDDEN_RAW_KEYS:
                key_hits.append({"row": row_idx, "path": key_path, "key": key})

    serialized = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    secret_hits = [pattern.pattern for pattern in SECRET_VALUE_PATTERNS if pattern.search(serialized)]
    status = "FAIL" if key_hits or secret_hits else "PASS"
    _add_check(
        checks,
        "replay_outcome_low_sensitivity_boundary",
        status,
        "raw or sensitive payload markers found" if status == "FAIL" else "no raw or sensitive payload markers found",
        {"key_hits": key_hits[:20], "secret_value_patterns": secret_hits},
    )


def _add_check(
    checks: List[Dict[str, Any]],
    name: str,
    status: str,
    message: str,
    evidence: Optional[Mapping[str, Any]] = None,
) -> None:
    checks.append(
        {
            "check": name,
            "status": status,
            "message": message,
            "evidence": dict(evidence or {}),
        }
    )


def _row_identity(row: Mapping[str, Any], row_number: int) -> Dict[str, Any]:
    return {
        "row_number": row_number,
        "stock_code": _safe_text(row.get("stock_code")),
        "eval_horizon": _safe_text(row.get("eval_horizon")),
        "outcome_join_key": _safe_text(row.get("outcome_join_key")),
    }


def _base_outcome_row(row: Mapping[str, Any], generated_at: str) -> Dict[str, Any]:
    dimension_fields = {}
    for name in DIMENSION_NAMES:
        dimension_fields[f"{name}_status"] = _safe_text(row.get(f"{name}_status"))
        dimension_fields[f"{name}_label"] = _safe_text(row.get(f"{name}_label"))
        dimension_fields[f"{name}_missing_reason"] = _safe_text(row.get(f"{name}_missing_reason"))

    return {
        "outcome_schema_version": OUTCOME_SCHEMA_VERSION,
        "generated_at": generated_at,
        "queue_schema_version": _safe_text(row.get("queue_schema_version")),
        "candidate_source": _safe_text(row.get("candidate_source")),
        "candidate_rank": _int_or_none(row.get("candidate_rank")),
        "trade_date": _safe_text(row.get("trade_date")),
        "stock_code": _safe_text(row.get("stock_code")),
        "stock_name": _safe_text(row.get("stock_name")),
        "model_used": _safe_text(row.get("model_used")),
        "model_sentiment_score": _numeric_or_none(row.get("model_sentiment_score")),
        "model_decision_type": _safe_text(row.get("model_decision_type")),
        "model_confidence_level": _safe_text(row.get("model_confidence_level")),
        "model_operation_advice": _safe_text(row.get("model_operation_advice")),
        "input_snapshot_hash": _safe_text(row.get("input_snapshot_hash")),
        "outcome_join_key": _safe_text(row.get("outcome_join_key")),
        "eval_horizon": _safe_text(row.get("eval_horizon")),
        "forward_trading_days": _int_or_none(row.get("forward_trading_days")),
        "invalidation_flags": row.get("invalidation_flags") if isinstance(row.get("invalidation_flags"), list) else [],
        "case_tags": _row_case_tags(row),
        **dimension_fields,
    }


def _build_price_index(price_rows: Sequence[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    index: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for row in price_rows:
        stock_code = _safe_text(row.get("stock_code"))
        trade_date = _safe_text(row.get("trade_date"))
        close = _numeric_or_none(row.get("close"))
        if not stock_code or not trade_date or close is None:
            continue
        index[stock_code][trade_date] = {
            "stock_code": stock_code,
            "trade_date": trade_date,
            "close": close,
        }
    return {
        stock_code: [by_date[trade_date] for trade_date in sorted(by_date)]
        for stock_code, by_date in index.items()
    }


def _lookup_stock_prices(
    price_index: Mapping[str, Sequence[Mapping[str, Any]]],
    stock_code: str,
) -> Sequence[Mapping[str, Any]]:
    for alias in _stock_code_aliases(stock_code):
        prices = price_index.get(alias)
        if prices:
            return prices
    return []


def _benchmark_return(
    price_index: Mapping[str, Sequence[Mapping[str, Any]]],
    trade_date: str,
    forward_days: int,
) -> Optional[float]:
    returns: List[float] = []
    for stock_prices in price_index.values():
        anchor_idx = _find_trade_date_index(stock_prices, trade_date)
        if anchor_idx is None or anchor_idx + forward_days >= len(stock_prices):
            continue
        anchor = stock_prices[anchor_idx]
        future = stock_prices[anchor_idx + forward_days]
        value = _return(_numeric_or_none(anchor.get("close")), _numeric_or_none(future.get("close")))
        if value is not None:
            returns.append(value)
    return _avg(returns)


def _stock_code_aliases(stock_code: str) -> List[str]:
    text = _safe_text(stock_code)
    if not text:
        return []
    upper = text.upper()
    aliases = [text, upper]
    normalized = upper.replace("_", ".")

    def add(value: str) -> None:
        if value and value not in aliases:
            aliases.append(value)

    if re.fullmatch(r"\d{6}", normalized):
        add(normalized)
        add(f"{normalized}.SH")
        add(f"{normalized}.SZ")
        add(f"SH{normalized}")
        add(f"SZ{normalized}")
    suffix_match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", normalized)
    if suffix_match:
        bare, exchange = suffix_match.groups()
        add(bare)
        add(f"{bare}.{exchange}")
        add(f"{exchange}{bare}")
    prefix_match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", normalized)
    if prefix_match:
        exchange, bare = prefix_match.groups()
        add(bare)
        add(f"{bare}.{exchange}")
        add(f"{exchange}{bare}")

    hk_match = re.fullmatch(r"(?:HK)?(\d{4,5})(?:\.HK)?", normalized)
    if hk_match:
        bare = hk_match.group(1).zfill(5)
        add(bare)
        add(bare[-4:])
        add(f"HK{bare}")
        add(f"{bare}.HK")
        add(f"{bare[-4:]}.HK")
    return aliases


def _find_trade_date_index(rows: Sequence[Mapping[str, Any]], trade_date: str) -> Optional[int]:
    for idx, row in enumerate(rows):
        if _safe_text(row.get("trade_date")) == trade_date:
            return idx
    return None


def _return(anchor_close: Optional[float], future_close: Optional[float]) -> Optional[float]:
    if anchor_close is None or future_close is None or anchor_close == 0:
        return None
    return round(future_close / anchor_close - 1.0, 10)


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL row: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: JSONL row is not an object")
            rows.append(value)
    return rows


def _load_price_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".jsonl":
        return _load_jsonl(path)
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            return []
        missing = REQUIRED_PRICE_FIELDS - set(reader.fieldnames)
        if missing:
            raise ValueError(f"{path}: missing price columns: {sorted(missing)}")
        return [dict(row) for row in reader]


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            file.write("\n")


def _write_json(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(json.dumps(list(rows), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_json_value(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _walk_keys(value: Any, prefix: str = "") -> List[tuple[str, str]]:
    hits: List[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            hits.append((path, key_text))
            hits.extend(_walk_keys(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(_walk_keys(child, f"{prefix}[{index}]"))
    return hits


def _normalize_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").lower()


def _outcome_file_date(rows: Sequence[Mapping[str, Any]], generated_at: Optional[datetime]) -> str:
    trade_dates = [_safe_text(row.get("trade_date")) for row in rows if _safe_text(row.get("trade_date"))]
    if trade_dates:
        return max(trade_dates).replace("-", "")
    return _iso_timestamp(generated_at)[:10].replace("-", "")


def _drop_none(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if value is not None}


def _summary_row(
    rows: Sequence[Mapping[str, Any]],
    *,
    eval_horizon: str,
    model_used: str,
    outcome_status: str,
    invalidation_flag: Optional[str] = None,
    flag_state: Optional[str] = None,
    dimension_name: Optional[str] = None,
    dimension_status: Optional[str] = None,
    dimension_label: Optional[str] = None,
    dimension_missing_reason: Optional[str] = None,
    case_tag_group: Optional[str] = None,
    case_tag: Optional[str] = None,
    model_decision_type: Optional[str] = None,
    model_sentiment_bucket: Optional[str] = None,
    model_confidence_level: Optional[str] = None,
) -> Dict[str, Any]:
    future_returns = [_numeric_or_none(row.get("future_return")) for row in rows]
    excess_returns = [_numeric_or_none(row.get("excess_return")) for row in rows]
    future_values = [value for value in future_returns if value is not None]
    excess_values = [value for value in excess_returns if value is not None]
    return _drop_none(
        {
            "eval_horizon": eval_horizon,
            "model_used": model_used,
            "outcome_status": outcome_status,
            "invalidation_flag": invalidation_flag,
            "flag_state": flag_state,
            "dimension_name": dimension_name,
            "dimension_status": dimension_status,
            "dimension_label": dimension_label,
            "dimension_missing_reason": dimension_missing_reason,
            "case_tag_group": case_tag_group,
            "case_tag": case_tag,
            "model_decision_type": model_decision_type,
            "model_sentiment_bucket": model_sentiment_bucket,
            "model_confidence_level": model_confidence_level,
            "n_rows": len(rows),
            "available_count": len(future_values),
            "available_ratio": round(len(future_values) / len(rows), 6) if rows else None,
            "avg_future_return": _avg(future_values),
            "median_future_return": _median(future_values),
            "avg_excess_return": _avg(excess_values),
            "median_excess_return": _median(excess_values),
            "win_rate": _rate(value > 0 for value in future_values),
            "excess_win_rate": _rate(value > 0 for value in excess_values),
        }
    )


def _row_invalidation_flags(row: Mapping[str, Any]) -> List[str]:
    flags = row.get("invalidation_flags")
    if not isinstance(flags, list):
        return []
    return sorted({_safe_text(flag) for flag in flags if _safe_text(flag)})


def _row_case_tags(row: Mapping[str, Any]) -> List[str]:
    tags = row.get("case_tags")
    if not isinstance(tags, list):
        return []
    return sorted({_safe_text(tag) for tag in tags if _safe_text(tag)})


def _case_tag_group(tag: str) -> str:
    if tag in {
        "fund_flow_available",
        "high_confidence",
        "quality_growth_available",
        "technical_strong",
        "theme_tailwind",
    }:
        return "bull"
    if tag in {
        "data_coverage_low",
        "flow_broke",
        "fund_flow_missing",
        "price_flow_hot",
        "price_hot_20d",
        "quality_growth_missing",
        "risk_flags",
        "technical_weak",
    }:
        return "bear"
    return "neutral"


def _avg(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 10)


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return round(float(median(values)), 10)


def _rate(values: Sequence[bool]) -> Optional[float]:
    items = list(values)
    if not items:
        return None
    return round(sum(1 for value in items if value) / len(items), 6)


def _delta(left: Any, right: Any) -> Optional[float]:
    left_value = _numeric_or_none(left)
    right_value = _numeric_or_none(right)
    if left_value is None or right_value is None:
        return None
    return round(left_value - right_value, 10)


def _effect_direction(delta_avg_excess_return: Optional[float]) -> Optional[str]:
    if delta_avg_excess_return is None:
        return None
    if delta_avg_excess_return < 0:
        return "hit_underperformed_miss"
    if delta_avg_excess_return > 0:
        return "hit_outperformed_miss"
    return "flat"


def _guard_policy_signal(
    hit_count: int,
    miss_count: int,
    delta_avg_excess_return: Optional[float],
    net_deweight_edge_after_cost: Optional[float],
    *,
    min_paired_samples: int,
    material_excess_delta: float,
) -> str:
    if hit_count <= 0 or miss_count <= 0:
        return "insufficient_pair"
    if delta_avg_excess_return is None:
        return "insufficient_return_data"
    if min(hit_count, miss_count) < min_paired_samples:
        return "low_sample_direction_only"
    threshold = abs(material_excess_delta)
    if net_deweight_edge_after_cost is not None and net_deweight_edge_after_cost >= threshold:
        return "exit_deweight_candidate"
    if net_deweight_edge_after_cost is not None and net_deweight_edge_after_cost > 0:
        return "watch_deweight_candidate"
    if delta_avg_excess_return >= threshold:
        return "no_exit_edge_observed"
    return "neutral_edge"


def _guard_directional_hint(delta_avg_excess_return: Optional[float]) -> Optional[str]:
    if delta_avg_excess_return is None:
        return None
    if delta_avg_excess_return < 0:
        return "flag_hit_worse"
    if delta_avg_excess_return > 0:
        return "flag_hit_better"
    return "flat"


def _guard_evidence_strength(hit_count: int, miss_count: int, min_paired_samples: int) -> str:
    paired_count = min(hit_count, miss_count)
    if paired_count <= 0:
        return "insufficient_pair"
    if paired_count >= max(min_paired_samples * 4, 20):
        return "strong"
    if paired_count >= min_paired_samples:
        return "usable"
    return "low_sample"


def _guard_gross_deweight_edge(delta_avg_excess_return: Optional[float]) -> Optional[float]:
    if delta_avg_excess_return is None:
        return None
    return round(-delta_avg_excess_return, 10)


def _guard_net_deweight_edge(
    delta_avg_excess_return: Optional[float],
    assumed_round_trip_cost_rate: float,
) -> Optional[float]:
    gross_edge = _guard_gross_deweight_edge(delta_avg_excess_return)
    if gross_edge is None:
        return None
    return round(gross_edge - assumed_round_trip_cost_rate, 10)


def _guard_cost_buffer_status(
    net_deweight_edge_after_cost: Optional[float],
    material_excess_delta: float,
) -> str:
    if net_deweight_edge_after_cost is None:
        return "missing_return_data"
    threshold = abs(material_excess_delta)
    if net_deweight_edge_after_cost >= threshold:
        return "material_after_cost"
    if net_deweight_edge_after_cost > 0:
        return "positive_after_cost"
    return "not_cost_covered"


def _bps_to_rate(value: Any) -> float:
    numeric = _numeric_or_none(value)
    if numeric is None:
        return 0.0
    return round(numeric / 10000.0, 10)


def _guard_recommended_action(policy_signal: str) -> str:
    return {
        "exit_deweight_candidate": "backtest_dynamic_exit_or_deweight",
        "watch_deweight_candidate": "monitor_and_backtest_with_costs",
        "no_exit_edge_observed": "do_not_deweight_from_this_flag",
        "neutral_edge": "keep_observing",
        "low_sample_direction_only": "collect_more_replay_data",
        "insufficient_pair": "collect_more_replay_data",
        "insufficient_return_data": "repair_replay_price_coverage",
    }.get(policy_signal, "review_manually")


def _guard_policy_rationale(policy_signal: str) -> str:
    return {
        "exit_deweight_candidate": "flag hits underperform misses by a material excess-return gap after assumed round-trip cost",
        "watch_deweight_candidate": "flag hits underperform misses after assumed round-trip cost, but the gap is below the material threshold",
        "no_exit_edge_observed": "flag hits outperform misses, so this flag is not an exit edge yet",
        "neutral_edge": "flag hit and miss groups do not clear the assumed round-trip cost buffer",
        "low_sample_direction_only": "both hit and miss groups exist, but paired sample counts are still low",
        "insufficient_pair": "hit or miss group is missing for this flag and horizon",
        "insufficient_return_data": "paired rows exist but return fields are not available",
    }.get(policy_signal, "manual review required")


def _sentiment_bucket(value: Any) -> str:
    score = _numeric_or_none(value)
    if score is None:
        return "missing_score"
    if score >= 80:
        return "score_80_plus"
    if score >= 70:
        return "score_70_79"
    if score >= 60:
        return "score_60_69"
    if score >= 50:
        return "score_50_59"
    return "score_lt_50"


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _iso_timestamp(value: Optional[datetime]) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve AI replay queue outcomes from point-in-time prices.")
    parser.add_argument("--queue-path", default=str(DEFAULT_QUEUE_PATH))
    parser.add_argument("--price-path", required=True)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--no-fail", action="store_true", help="Do not exit non-zero when the outcome audit fails.")
    args = parser.parse_args(argv)

    paths = write_replay_outcome_outputs(args.queue_path, args.price_path, output_dir=args.output_dir)
    if not paths:
        print("ai_snapshot_replay_outcome rows=0 outputs=")
        return 1
    rows = _load_jsonl(paths[0])
    statuses = defaultdict(int)
    for row in rows:
        statuses[_safe_text(row.get("outcome_status"))] += 1
    audit = audit_replay_outcomes(rows)
    print(
        "ai_snapshot_replay_outcome "
        f"rows={len(rows)} "
        f"statuses={dict(statuses)} "
        f"audit_status={audit['overall_status']} "
        f"outputs={', '.join(str(path) for path in paths)}"
    )
    return 0 if args.no_fail or audit["overall_status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
