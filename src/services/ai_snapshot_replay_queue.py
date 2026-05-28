# -*- coding: utf-8 -*-
"""Build outcome replay queue rows from hosted AI candidate snapshots."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


QUEUE_SCHEMA_VERSION = "1.1"
QUEUE_AUDIT_SCHEMA_VERSION = "1.0"
DEFAULT_SNAPSHOT_PATH = Path("reports") / "ai_snapshot" / "stock_ai_candidate_snapshot_latest.jsonl"
DEFAULT_OUTPUT_DIR = Path("reports") / "ai_snapshot"
REPLAY_HORIZONS = (
    ("1d", 1),
    ("3d", 3),
    ("1w", 5),
    ("1m", 20),
    ("1q", 60),
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
REQUIRED_QUEUE_COLUMNS = {
    "queue_schema_version",
    "snapshot_schema_version",
    "candidate_source",
    "candidate_rank",
    "trade_date",
    "stock_code",
    "model_used",
    "input_snapshot_hash",
    "eval_horizon",
    "forward_trading_days",
    "outcome_status",
    "outcome_join_key",
}
KNOWN_INVALIDATION_FLAGS = {
    "data_coverage_review",
    "flow_broke_proxy",
    "fund_flow_missing",
    "fund_flow_not_supported",
    "hard_risk_hit_proxy",
    "price_flow_hot_proxy",
    "price_hot_20d_proxy",
}
KNOWN_CASE_TAGS = {
    "data_coverage_low",
    "flow_broke",
    "fund_flow_available",
    "fund_flow_missing",
    "high_confidence",
    "price_flow_hot",
    "price_hot_20d",
    "quality_growth_available",
    "quality_growth_missing",
    "risk_flags",
    "technical_strong",
    "technical_weak",
    "theme_tailwind",
}
FORBIDDEN_RAW_KEYS = {
    "api_key",
    "authorization",
    "belong_boards",
    "board_name",
    "board_names",
    "boards",
    "cookie",
    "inflow_5d",
    "main_net_inflow",
    "news_content",
    "news_retrieval_content",
    "password",
    "secret",
    "token",
}
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
)


def build_replay_queue_rows(
    snapshot_rows: Sequence[Mapping[str, Any]],
    *,
    generated_at: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Expand candidate snapshots into deterministic future-outcome queue rows."""
    timestamp = _iso_timestamp(generated_at)
    rows: List[Dict[str, Any]] = []
    for snapshot in snapshot_rows:
        dimensions = _dimension_map(snapshot)
        base = _base_queue_row(snapshot, dimensions, timestamp)
        for horizon, forward_days in REPLAY_HORIZONS:
            row = dict(base)
            row.update(
                {
                    "eval_horizon": horizon,
                    "forward_trading_days": forward_days,
                    "outcome_status": "pending_future_price",
                    "outcome_join_key": f"{base['input_snapshot_hash']}:{horizon}",
                }
            )
            rows.append(_drop_empty(row))
    return rows


def write_replay_queue_outputs(
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
    *,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    generated_at: Optional[datetime] = None,
) -> List[Path]:
    """Write latest and dated JSONL/CSV replay queue artifacts."""
    snapshot_file = Path(snapshot_path)
    snapshot_rows = _load_jsonl(snapshot_file)
    queue_rows = build_replay_queue_rows(snapshot_rows, generated_at=generated_at)
    if not queue_rows:
        return []

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    queue_date = _queue_file_date(queue_rows, generated_at)

    latest_jsonl = target_dir / "stock_ai_candidate_replay_queue_latest.jsonl"
    dated_jsonl = target_dir / f"stock_ai_candidate_replay_queue_{queue_date}.jsonl"
    latest_csv = target_dir / "stock_ai_candidate_replay_queue_latest.csv"
    dated_csv = target_dir / f"stock_ai_candidate_replay_queue_{queue_date}.csv"

    _write_jsonl(latest_jsonl, queue_rows)
    if dated_jsonl != latest_jsonl:
        _write_jsonl(dated_jsonl, queue_rows)
    _write_csv(latest_csv, queue_rows)
    if dated_csv != latest_csv:
        _write_csv(dated_csv, queue_rows)
    return [latest_jsonl, dated_jsonl, latest_csv, dated_csv]


def audit_replay_queue(
    snapshot_rows: Sequence[Mapping[str, Any]],
    queue_rows: Sequence[Mapping[str, Any]],
    *,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Audit replay queue coverage, join keys, and low-sensitivity boundaries."""
    checks: List[Dict[str, Any]] = []
    snapshot_hashes = {
        _safe_text(row.get("input_snapshot_hash"))
        for row in snapshot_rows
        if _safe_text(row.get("input_snapshot_hash"))
    }
    queue_hashes = [
        _safe_text(row.get("input_snapshot_hash"))
        for row in queue_rows
        if _safe_text(row.get("input_snapshot_hash"))
    ]

    _add_check(
        checks,
        "replay_queue_rows_non_empty",
        "PASS" if queue_rows else "FAIL",
        f"replay queue row count={len(queue_rows)}",
        {"row_count": len(queue_rows), "snapshot_candidate_count": len(snapshot_hashes)},
    )
    _audit_required_columns(checks, queue_rows)
    _audit_horizon_coverage(checks, snapshot_hashes, queue_rows)
    _audit_join_keys(checks, queue_rows)
    _audit_snapshot_hash_links(checks, snapshot_hashes, queue_hashes)
    _audit_outcome_status(checks, queue_rows)
    _audit_invalidation_flags(checks, queue_rows)
    _audit_case_tags(checks, queue_rows)
    _audit_dated_queue_file(checks, queue_rows, output_dir)
    _audit_low_sensitivity(checks, queue_rows)

    status_counts = dict(Counter(check["status"] for check in checks))
    overall_status = "FAIL" if status_counts.get("FAIL") else ("WARN" if status_counts.get("WARN") else "PASS")
    trade_dates = sorted({_safe_text(row.get("trade_date")) for row in queue_rows if _safe_text(row.get("trade_date"))})
    horizons = sorted(
        {
            _safe_text(row.get("eval_horizon"))
            for row in queue_rows
            if _safe_text(row.get("eval_horizon"))
        }
    )
    return {
        "audit_schema_version": QUEUE_AUDIT_SCHEMA_VERSION,
        "generated_at": _iso_timestamp(generated_at),
        "overall_status": overall_status,
        "status_counts": status_counts,
        "row_count": len(queue_rows),
        "candidate_count": len(snapshot_hashes),
        "trade_dates": trade_dates,
        "latest_trade_date": max(trade_dates) if trade_dates else None,
        "horizons": horizons,
        "checks": checks,
    }


def write_replay_queue_audit_outputs(
    audit: Mapping[str, Any],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> List[Path]:
    """Write latest and dated replay queue audit JSON/CSV artifacts."""
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    audit_date = _queue_audit_file_date(audit)

    latest_json = target_dir / "stock_ai_candidate_replay_queue_audit_latest.json"
    dated_json = target_dir / f"stock_ai_candidate_replay_queue_audit_{audit_date}.json"
    latest_csv = target_dir / "stock_ai_candidate_replay_queue_audit_latest.csv"
    dated_csv = target_dir / f"stock_ai_candidate_replay_queue_audit_{audit_date}.csv"

    payload = json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True)
    latest_json.write_text(payload + "\n", encoding="utf-8")
    if dated_json != latest_json:
        dated_json.write_text(payload + "\n", encoding="utf-8")
    _write_checks_csv(latest_csv, audit.get("checks", []))
    if dated_csv != latest_csv:
        _write_checks_csv(dated_csv, audit.get("checks", []))
    return [latest_json, dated_json, latest_csv, dated_csv]


def _base_queue_row(
    snapshot: Mapping[str, Any],
    dimensions: Mapping[str, Mapping[str, Any]],
    generated_at: str,
) -> Dict[str, Any]:
    model_result = snapshot.get("model_result") if isinstance(snapshot.get("model_result"), Mapping) else {}
    dimension_fields = {}
    for name in DIMENSION_NAMES:
        dimension = dimensions.get(name, {})
        dimension_fields[f"{name}_status"] = _safe_text(dimension.get("status"))
        dimension_fields[f"{name}_label"] = _safe_text(dimension.get("label"))
        dimension_fields[f"{name}_missing_reason"] = _safe_text(dimension.get("missing_reason"))

    return {
        "queue_schema_version": QUEUE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "snapshot_schema_version": _safe_text(snapshot.get("schema_version")),
        "snapshot_kind": _safe_text(snapshot.get("snapshot_kind")),
        "candidate_source": _safe_text(snapshot.get("candidate_source")),
        "candidate_rank": _int_or_none(snapshot.get("candidate_rank")),
        "trade_date": _safe_text(snapshot.get("trade_date")),
        "stock_code": _safe_text(snapshot.get("stock_code")),
        "stock_name": _safe_text(snapshot.get("stock_name")),
        "report_language": _safe_text(snapshot.get("report_language")),
        "model_used": _safe_text(snapshot.get("model_used")),
        "model_sentiment_score": _numeric_or_none(model_result.get("sentiment_score")),
        "model_decision_type": _safe_text(model_result.get("decision_type")),
        "model_confidence_level": _safe_text(model_result.get("confidence_level")),
        "model_operation_advice": _safe_text(model_result.get("operation_advice")),
        "input_snapshot_hash": _safe_text(snapshot.get("input_snapshot_hash")),
        "news_result_count": _int_or_none(snapshot.get("news_result_count")),
        "factor_warning_codes": _warning_codes(snapshot),
        "invalidation_flags": _invalidation_flags(dimensions),
        "case_tags": _case_tags(dimensions),
        **dimension_fields,
    }


def _audit_required_columns(checks: List[Dict[str, Any]], queue_rows: Sequence[Mapping[str, Any]]) -> None:
    missing: List[Dict[str, Any]] = []
    for idx, row in enumerate(queue_rows, start=1):
        missing_columns = sorted(column for column in REQUIRED_QUEUE_COLUMNS if row.get(column) in (None, "", [], {}))
        if missing_columns:
            missing.append(
                {
                    "row_number": idx,
                    "stock_code": _safe_text(row.get("stock_code")),
                    "missing": missing_columns,
                }
            )
    _add_check(
        checks,
        "replay_queue_required_columns",
        "FAIL" if missing else "PASS",
        "required replay queue columns are missing" if missing else "all required replay queue columns are present",
        {"required_columns": sorted(REQUIRED_QUEUE_COLUMNS), "missing": missing[:20]},
    )


def _audit_horizon_coverage(
    checks: List[Dict[str, Any]],
    snapshot_hashes: set[str],
    queue_rows: Sequence[Mapping[str, Any]],
) -> None:
    expected_horizons = {horizon for horizon, _ in REPLAY_HORIZONS}
    by_hash: Dict[str, set[str]] = {}
    for row in queue_rows:
        digest = _safe_text(row.get("input_snapshot_hash"))
        if not digest:
            continue
        by_hash.setdefault(digest, set()).add(_safe_text(row.get("eval_horizon")))

    missing_candidates = sorted(snapshot_hashes - set(by_hash))
    unexpected_candidates = sorted(set(by_hash) - snapshot_hashes)
    horizon_gaps = [
        {
            "input_snapshot_hash": digest,
            "missing_horizons": sorted(expected_horizons - horizons),
            "unexpected_horizons": sorted(horizons - expected_horizons),
        }
        for digest, horizons in sorted(by_hash.items())
        if horizons != expected_horizons
    ]
    expected_row_count = len(snapshot_hashes) * len(REPLAY_HORIZONS)
    row_count_ok = len(queue_rows) == expected_row_count
    status = (
        "PASS"
        if not missing_candidates and not unexpected_candidates and not horizon_gaps and row_count_ok
        else "FAIL"
    )
    _add_check(
        checks,
        "replay_queue_candidate_horizon_coverage",
        status,
        "each snapshot candidate has every replay horizon" if status == "PASS" else "replay horizon coverage mismatch",
        {
            "expected_horizons": sorted(expected_horizons),
            "expected_row_count": expected_row_count,
            "actual_row_count": len(queue_rows),
            "missing_candidates": missing_candidates[:20],
            "unexpected_candidates": unexpected_candidates[:20],
            "horizon_gaps": horizon_gaps[:20],
        },
    )


def _audit_join_keys(checks: List[Dict[str, Any]], queue_rows: Sequence[Mapping[str, Any]]) -> None:
    keys = [_safe_text(row.get("outcome_join_key")) for row in queue_rows]
    duplicate_keys = sorted(key for key, count in Counter(keys).items() if key and count > 1)
    invalid_rows = []
    for idx, row in enumerate(queue_rows, start=1):
        expected = f"{_safe_text(row.get('input_snapshot_hash'))}:{_safe_text(row.get('eval_horizon'))}"
        if _safe_text(row.get("outcome_join_key")) != expected:
            invalid_rows.append(
                {
                    "row_number": idx,
                    "stock_code": _safe_text(row.get("stock_code")),
                    "expected": expected,
                    "actual": _safe_text(row.get("outcome_join_key")),
                }
            )
    status = "FAIL" if duplicate_keys or invalid_rows else "PASS"
    _add_check(
        checks,
        "replay_queue_join_keys",
        status,
        (
            "outcome join keys are unique and deterministic"
            if status == "PASS"
            else "invalid replay queue join keys found"
        ),
        {"duplicate_keys": duplicate_keys[:20], "invalid_rows": invalid_rows[:20]},
    )


def _audit_snapshot_hash_links(
    checks: List[Dict[str, Any]],
    snapshot_hashes: set[str],
    queue_hashes: Sequence[str],
) -> None:
    queue_hash_set = {digest for digest in queue_hashes if digest}
    missing = sorted(snapshot_hashes - queue_hash_set)
    unexpected = sorted(queue_hash_set - snapshot_hashes)
    status = "FAIL" if missing or unexpected else "PASS"
    _add_check(
        checks,
        "replay_queue_snapshot_hash_links",
        status,
        (
            "replay queue references exactly the snapshot input hashes"
            if status == "PASS"
            else "replay queue hash links mismatch"
        ),
        {
            "snapshot_hash_count": len(snapshot_hashes),
            "queue_hash_count": len(queue_hash_set),
            "missing_hashes": missing[:20],
            "unexpected_hashes": unexpected[:20],
        },
    )


def _audit_outcome_status(checks: List[Dict[str, Any]], queue_rows: Sequence[Mapping[str, Any]]) -> None:
    statuses = Counter(_safe_text(row.get("outcome_status")) for row in queue_rows)
    bad_rows = [
        {
            "row_number": idx,
            "stock_code": _safe_text(row.get("stock_code")),
            "outcome_status": _safe_text(row.get("outcome_status")),
        }
        for idx, row in enumerate(queue_rows, start=1)
        if _safe_text(row.get("outcome_status")) != "pending_future_price"
    ]
    _add_check(
        checks,
        "replay_queue_outcome_status_pending",
        "FAIL" if bad_rows else "PASS",
        "all replay rows are pending future price joins" if not bad_rows else "non-pending replay rows found",
        {"status_counts": dict(statuses), "bad_rows": bad_rows[:20]},
    )


def _audit_invalidation_flags(checks: List[Dict[str, Any]], queue_rows: Sequence[Mapping[str, Any]]) -> None:
    unknown: List[Dict[str, Any]] = []
    flag_counts: Counter[str] = Counter()
    for idx, row in enumerate(queue_rows, start=1):
        flags = row.get("invalidation_flags") if isinstance(row.get("invalidation_flags"), list) else []
        for flag in flags:
            flag_text = _safe_text(flag)
            flag_counts[flag_text] += 1
            if flag_text not in KNOWN_INVALIDATION_FLAGS:
                unknown.append({"row_number": idx, "stock_code": _safe_text(row.get("stock_code")), "flag": flag_text})
    _add_check(
        checks,
        "replay_queue_invalidation_flags",
        "WARN" if unknown else "PASS",
        "invalidation flags use known labels" if not unknown else "unknown replay invalidation flags found",
        {"flag_counts": dict(flag_counts), "unknown_flags": unknown[:20]},
    )


def _audit_case_tags(checks: List[Dict[str, Any]], queue_rows: Sequence[Mapping[str, Any]]) -> None:
    unknown: List[Dict[str, Any]] = []
    tag_counts: Counter[str] = Counter()
    tagged_rows = 0
    for idx, row in enumerate(queue_rows, start=1):
        tags = row.get("case_tags") if isinstance(row.get("case_tags"), list) else []
        if tags:
            tagged_rows += 1
        for tag in tags:
            tag_text = _safe_text(tag)
            tag_counts[tag_text] += 1
            if tag_text not in KNOWN_CASE_TAGS:
                unknown.append({"row_number": idx, "stock_code": _safe_text(row.get("stock_code")), "tag": tag_text})
    _add_check(
        checks,
        "replay_queue_case_tags",
        "WARN" if unknown else "PASS",
        "case tags use known labels" if not unknown else "unknown replay case tags found",
        {"tag_counts": dict(tag_counts), "tagged_rows": tagged_rows, "unknown_tags": unknown[:20]},
    )


def _audit_dated_queue_file(
    checks: List[Dict[str, Any]],
    queue_rows: Sequence[Mapping[str, Any]],
    output_dir: Path | str,
) -> None:
    trade_dates = sorted({_safe_text(row.get("trade_date")) for row in queue_rows if _safe_text(row.get("trade_date"))})
    latest_trade_date = max(trade_dates) if trade_dates else ""
    expected_jsonl = Path(output_dir) / f"stock_ai_candidate_replay_queue_{latest_trade_date.replace('-', '')}.jsonl"
    expected_csv = Path(output_dir) / f"stock_ai_candidate_replay_queue_{latest_trade_date.replace('-', '')}.csv"
    exists = bool(latest_trade_date and expected_jsonl.exists() and expected_csv.exists())
    _add_check(
        checks,
        "replay_queue_dated_files",
        "PASS" if exists else "FAIL",
        "dated replay queue files match latest trade_date" if exists else "dated replay queue files are missing",
        {
            "latest_trade_date": latest_trade_date,
            "expected_jsonl": str(expected_jsonl),
            "expected_csv": str(expected_csv),
            "jsonl_exists": expected_jsonl.exists(),
            "csv_exists": expected_csv.exists(),
        },
    )


def _audit_low_sensitivity(checks: List[Dict[str, Any]], queue_rows: Sequence[Mapping[str, Any]]) -> None:
    key_hits: List[Dict[str, Any]] = []
    for row_idx, row in enumerate(queue_rows, start=1):
        for key_path, key in _walk_keys(row):
            if _normalize_key(key) in FORBIDDEN_RAW_KEYS:
                key_hits.append({"row": row_idx, "path": key_path, "key": key})
    serialized = json.dumps(queue_rows, ensure_ascii=False, sort_keys=True)
    secret_hits = [pattern.pattern for pattern in SECRET_VALUE_PATTERNS if pattern.search(serialized)]
    status = "FAIL" if key_hits or secret_hits else "PASS"
    _add_check(
        checks,
        "replay_queue_low_sensitivity_boundary",
        status,
        (
            "raw or sensitive payload markers found"
            if status == "FAIL"
            else "no raw or sensitive replay payload markers found"
        ),
        {"key_hits": key_hits[:20], "secret_value_patterns": secret_hits},
    )


def _add_check(
    checks: List[Dict[str, Any]],
    check_id: str,
    status: str,
    message: str,
    evidence: Optional[Mapping[str, Any]] = None,
) -> None:
    checks.append(
        {
            "check_id": check_id,
            "status": status,
            "message": message,
            "evidence": dict(evidence or {}),
        }
    )


def _invalidation_flags(dimensions: Mapping[str, Mapping[str, Any]]) -> List[str]:
    flags: List[str] = []
    de_risk_label = _dimension_label(dimensions, "de_risk")
    price_heat_label = _dimension_label(dimensions, "price_heat")
    fund_flow_status = _dimension_status(dimensions, "fund_flow")
    risk_label = _dimension_label(dimensions, "risk")
    data_coverage_label = _dimension_label(dimensions, "data_coverage")

    if "flow_broke" in de_risk_label:
        flags.append("flow_broke_proxy")
    if "price_flow_hot" in de_risk_label:
        flags.append("price_flow_hot_proxy")
    if price_heat_label == "overheated":
        flags.append("price_hot_20d_proxy")
    if risk_label == "has_risk_flags":
        flags.append("hard_risk_hit_proxy")
    if fund_flow_status == "missing":
        flags.append("fund_flow_missing")
    elif fund_flow_status == "not_supported":
        flags.append("fund_flow_not_supported")
    if data_coverage_label and data_coverage_label not in {"high", "medium"}:
        flags.append("data_coverage_review")
    return sorted(set(flags))


def _case_tags(dimensions: Mapping[str, Mapping[str, Any]]) -> List[str]:
    tags: List[str] = []
    technical_label = _dimension_label(dimensions, "technical_score")
    price_heat_label = _dimension_label(dimensions, "price_heat")
    industry_theme_label = _dimension_label(dimensions, "industry_theme")
    quality_growth_status = _dimension_status(dimensions, "quality_growth")
    quality_growth_label = _dimension_label(dimensions, "quality_growth")
    fund_flow_status = _dimension_status(dimensions, "fund_flow")
    fund_flow_label = _dimension_label(dimensions, "fund_flow")
    de_risk_label = _dimension_label(dimensions, "de_risk")
    data_coverage_label = _dimension_label(dimensions, "data_coverage")
    risk_label = _dimension_label(dimensions, "risk")
    confidence_label = _dimension_label(dimensions, "confidence")

    if technical_label == "strong":
        tags.append("technical_strong")
    elif technical_label == "weak":
        tags.append("technical_weak")
    if price_heat_label in {"extended", "overheated"}:
        tags.append("price_hot_20d")
    if industry_theme_label == "theme_tailwind":
        tags.append("theme_tailwind")
    if quality_growth_status == "available" or quality_growth_label == "available":
        tags.append("quality_growth_available")
    elif quality_growth_status == "missing":
        tags.append("quality_growth_missing")
    if fund_flow_status == "available" and fund_flow_label in {"available", "supportive"}:
        tags.append("fund_flow_available")
    elif fund_flow_status == "missing":
        tags.append("fund_flow_missing")
    if "flow_broke" in de_risk_label:
        tags.append("flow_broke")
    if "price_flow_hot" in de_risk_label:
        tags.append("price_flow_hot")
    if data_coverage_label == "low":
        tags.append("data_coverage_low")
    if risk_label == "has_risk_flags":
        tags.append("risk_flags")
    if confidence_label == "high":
        tags.append("high_confidence")
    return sorted(set(tags))


def _warning_codes(snapshot: Mapping[str, Any]) -> List[str]:
    warnings: List[str] = []
    factor_snapshot = snapshot.get("factor_snapshot") if isinstance(snapshot.get("factor_snapshot"), Mapping) else {}
    warnings.extend(_safe_string_list(factor_snapshot.get("warnings")))
    coverage = snapshot.get("data_coverage") if isinstance(snapshot.get("data_coverage"), Mapping) else {}
    for summary in coverage.values():
        if isinstance(summary, Mapping):
            warnings.extend(_safe_string_list(summary.get("warnings")))
            warnings.extend(_safe_string_list(summary.get("missing_reasons")))
    return sorted({warning for warning in warnings if warning})


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


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            file.write("\n")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _write_checks_csv(path: Path, checks: Any) -> None:
    rows = checks if isinstance(checks, list) else []
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["check_id", "status", "message", "evidence_json"])
        writer.writeheader()
        for check in rows:
            check_map = check if isinstance(check, Mapping) else {}
            writer.writerow(
                {
                    "check_id": check_map.get("check_id", ""),
                    "status": check_map.get("status", ""),
                    "message": check_map.get("message", ""),
                    "evidence_json": json.dumps(check_map.get("evidence", {}), ensure_ascii=False, sort_keys=True),
                }
            )


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _queue_file_date(rows: Sequence[Mapping[str, Any]], generated_at: Optional[datetime]) -> str:
    trade_dates = [_safe_text(row.get("trade_date")) for row in rows if _safe_text(row.get("trade_date"))]
    if trade_dates:
        return max(trade_dates).replace("-", "")
    return _iso_timestamp(generated_at)[:10].replace("-", "")


def _queue_audit_file_date(audit: Mapping[str, Any]) -> str:
    latest_trade_date = _safe_text(audit.get("latest_trade_date"))
    if latest_trade_date:
        return latest_trade_date.replace("-", "")
    generated_at = _safe_text(audit.get("generated_at"))
    if generated_at:
        return generated_at[:10].replace("-", "")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _dimension_map(snapshot: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    factor_snapshot = snapshot.get("factor_snapshot") if isinstance(snapshot.get("factor_snapshot"), Mapping) else {}
    dimensions = factor_snapshot.get("dimensions") if isinstance(factor_snapshot, Mapping) else []
    result: Dict[str, Mapping[str, Any]] = {}
    if not isinstance(dimensions, list):
        return result
    for item in dimensions:
        if not isinstance(item, Mapping):
            continue
        name = _safe_text(item.get("name"))
        if name:
            result[name] = item
    return result


def _dimension_label(dimensions: Mapping[str, Mapping[str, Any]], name: str) -> str:
    return _safe_text(dimensions.get(name, {}).get("label"))


def _dimension_status(dimensions: Mapping[str, Mapping[str, Any]], name: str) -> str:
    return _safe_text(dimensions.get(name, {}).get("status"))


def _drop_empty(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def _safe_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [_safe_text(item) for item in value if _safe_text(item)]


def _walk_keys(value: Any, path: str = "$") -> List[tuple[str, str]]:
    hits: List[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            key_path = f"{path}.{key_text}"
            hits.append((key_path, key_text))
            hits.extend(_walk_keys(item, key_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            hits.extend(_walk_keys(item, f"{path}[{idx}]"))
    return hits


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


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
    parser = argparse.ArgumentParser(description="Build AI candidate future-outcome replay queue artifacts.")
    parser.add_argument("--snapshot-path", default=str(DEFAULT_SNAPSHOT_PATH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--no-fail", action="store_true", help="Return exit code 0 even when replay queue audit fails.")
    args = parser.parse_args(argv)

    snapshot_rows = _load_jsonl(Path(args.snapshot_path))
    paths = write_replay_queue_outputs(args.snapshot_path, output_dir=args.output_dir)
    if not paths:
        print("ai_snapshot_replay_queue rows=0 outputs=")
        return 1
    rows = _load_jsonl(paths[0])
    audit = audit_replay_queue(snapshot_rows, rows, output_dir=args.output_dir)
    audit_paths = write_replay_queue_audit_outputs(audit, args.output_dir)
    candidate_count = len(
        {
            _safe_text(row.get("input_snapshot_hash"))
            for row in rows
            if _safe_text(row.get("input_snapshot_hash"))
        }
    )
    print(
        "ai_snapshot_replay_queue "
        f"rows={len(rows)} "
        f"candidates={candidate_count} "
        f"horizons={len(REPLAY_HORIZONS)} "
        f"audit_status={audit['overall_status']} "
        f"outputs={', '.join(str(path) for path in paths + audit_paths)}"
    )
    return 0 if args.no_fail or audit["overall_status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
