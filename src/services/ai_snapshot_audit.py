# -*- coding: utf-8 -*-
"""PASS/WARN/FAIL audit for hosted AI candidate snapshot artifacts."""

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


AUDIT_SCHEMA_VERSION = "1.0"
DEFAULT_SNAPSHOT_PATH = Path("reports") / "ai_snapshot" / "stock_ai_candidate_snapshot_latest.jsonl"
DEFAULT_OUTPUT_DIR = Path("reports") / "ai_snapshot"
EXPECTED_SNAPSHOT_SCHEMA_VERSION = "1.1"
EXPECTED_MODEL = "deepseek/deepseek-v4-pro"
REQUIRED_DIMENSIONS = {
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
}
EXPECTED_COVERAGE_BLOCKS = (
    "quote",
    "daily_bars",
    "technical",
    "fundamentals",
    "factor_snapshot",
    "news",
)
CRITICAL_COVERAGE_BLOCKS = {
    "quote",
    "daily_bars",
    "technical",
    "factor_snapshot",
}
GOOD_COVERAGE_STATUSES = {
    "available",
    "not_supported",
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
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def audit_snapshot(
    snapshot_path: Path | str = DEFAULT_SNAPSHOT_PATH,
    *,
    created_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Audit a hosted AI candidate snapshot JSONL file."""
    path = Path(snapshot_path)
    rows, parse_errors = _load_jsonl(path)
    checks: List[Dict[str, Any]] = []

    _add_check(
        checks,
        "snapshot_file_exists",
        "PASS" if path.exists() else "FAIL",
        f"snapshot path {'exists' if path.exists() else 'is missing'}",
        {"path": str(path)},
    )
    _add_check(
        checks,
        "snapshot_jsonl_parseable",
        "FAIL" if parse_errors else "PASS",
        "snapshot JSONL parse errors found" if parse_errors else "snapshot JSONL parsed successfully",
        {"parse_errors": parse_errors[:10], "error_count": len(parse_errors)},
    )
    _add_check(
        checks,
        "snapshot_rows_non_empty",
        "PASS" if rows else "FAIL",
        f"snapshot row count={len(rows)}",
        {"row_count": len(rows)},
    )

    if rows:
        _audit_schema_versions(checks, rows)
        _audit_hashes(checks, rows)
        _audit_models(checks, rows)
        _audit_candidate_ranks(checks, rows)
        _audit_trade_dates(checks, rows, path)
        _audit_required_dimensions(checks, rows)
        _audit_data_coverage(checks, rows)
        _audit_low_sensitivity(checks, rows)

    status_counts = dict(Counter(check["status"] for check in checks))
    overall_status = "FAIL" if status_counts.get("FAIL") else ("WARN" if status_counts.get("WARN") else "PASS")
    trade_dates = sorted(
        {
            _safe_text(row.get("trade_date"))
            for row in rows
            if _safe_text(row.get("trade_date"))
        }
    )
    generated_at = _iso_timestamp(created_at)
    return {
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "snapshot_path": str(path),
        "overall_status": overall_status,
        "status_counts": status_counts,
        "row_count": len(rows),
        "trade_dates": trade_dates,
        "latest_trade_date": max(trade_dates) if trade_dates else None,
        "schema_versions": sorted(
            {
                _safe_text(row.get("schema_version"))
                for row in rows
                if _safe_text(row.get("schema_version"))
            }
        ),
        "models": sorted(
            {
                _safe_text(row.get("model_used"))
                for row in rows
                if _safe_text(row.get("model_used"))
            }
        ),
        "checks": checks,
    }


def write_audit_outputs(
    audit: Mapping[str, Any],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> List[Path]:
    """Write latest and dated audit JSON/CSV artifacts."""
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    audit_date = _audit_file_date(audit)

    latest_json = target_dir / "stock_ai_candidate_snapshot_audit_latest.json"
    dated_json = target_dir / f"stock_ai_candidate_snapshot_audit_{audit_date}.json"
    latest_csv = target_dir / "stock_ai_candidate_snapshot_audit_latest.csv"
    dated_csv = target_dir / f"stock_ai_candidate_snapshot_audit_{audit_date}.csv"

    payload = json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True)
    latest_json.write_text(payload + "\n", encoding="utf-8")
    if dated_json != latest_json:
        dated_json.write_text(payload + "\n", encoding="utf-8")

    _write_checks_csv(latest_csv, audit.get("checks", []))
    if dated_csv != latest_csv:
        _write_checks_csv(dated_csv, audit.get("checks", []))

    return [latest_json, dated_json, latest_csv, dated_csv]


def _load_jsonl(path: Path) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    if not path.exists():
        return rows, parse_errors
    with path.open("r", encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                parse_errors.append({"line": line_no, "error": str(exc)})
                continue
            if isinstance(value, dict):
                rows.append(value)
            else:
                parse_errors.append({"line": line_no, "error": "row is not a JSON object"})
    return rows, parse_errors


def _audit_schema_versions(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    versions = [_safe_text(row.get("schema_version")) for row in rows]
    missing = [idx + 1 for idx, value in enumerate(versions) if not value]
    unexpected = sorted({value for value in versions if value and value != EXPECTED_SNAPSHOT_SCHEMA_VERSION})
    if missing:
        status = "FAIL"
        message = "snapshot rows are missing schema_version"
    elif unexpected:
        status = "WARN"
        message = "snapshot rows use unexpected schema versions"
    else:
        status = "PASS"
        message = f"all rows use schema_version={EXPECTED_SNAPSHOT_SCHEMA_VERSION}"
    _add_check(
        checks,
        "snapshot_schema_version",
        status,
        message,
        {"expected": EXPECTED_SNAPSHOT_SCHEMA_VERSION, "unexpected": unexpected, "missing_rows": missing[:20]},
    )


def _audit_hashes(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    hashes = [_safe_text(row.get("input_snapshot_hash")) for row in rows]
    invalid_rows = [
        _row_identity(row, idx)
        for idx, row in enumerate(rows, start=1)
        if not HASH_RE.fullmatch(_safe_text(row.get("input_snapshot_hash")))
    ]
    duplicate_hashes = sorted(hash_value for hash_value, count in Counter(hashes).items() if hash_value and count > 1)
    if invalid_rows:
        status = "FAIL"
        message = "invalid or missing input_snapshot_hash values found"
    elif duplicate_hashes:
        status = "FAIL"
        message = "duplicate input_snapshot_hash values found"
    else:
        status = "PASS"
        message = "all input_snapshot_hash values are valid unique SHA-256 hex"
    _add_check(
        checks,
        "input_snapshot_hash",
        status,
        message,
        {
            "invalid_rows": invalid_rows[:20],
            "duplicate_hashes": duplicate_hashes[:20],
            "unique_hash_count": len({hash_value for hash_value in hashes if hash_value}),
        },
    )


def _audit_models(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    models = [_safe_text(row.get("model_used")) for row in rows]
    missing_rows = [_row_identity(row, idx) for idx, row in enumerate(rows, start=1) if not _safe_text(row.get("model_used"))]
    bad_flash = sorted({model for model in models if "flash" in model.lower()})
    bad_gemini = sorted({model for model in models if "gemini" in model.lower()})
    unexpected = sorted({model for model in models if model and model != EXPECTED_MODEL})
    if missing_rows or bad_flash or bad_gemini:
        status = "FAIL"
        message = "snapshot contains missing, flash, or Gemini model markers"
    elif unexpected:
        status = "WARN"
        message = "snapshot model differs from configured DeepSeek pro target"
    else:
        status = "PASS"
        message = f"all rows use {EXPECTED_MODEL}"
    _add_check(
        checks,
        "model_used_deepseek_pro",
        status,
        message,
        {
            "expected": EXPECTED_MODEL,
            "models": sorted({model for model in models if model}),
            "missing_rows": missing_rows[:20],
            "flash_models": bad_flash,
            "gemini_models": bad_gemini,
            "unexpected_models": unexpected,
        },
    )


def _audit_candidate_ranks(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    ranks = [_int_or_none(row.get("candidate_rank")) for row in rows]
    expected = list(range(1, len(rows) + 1))
    status = "PASS" if ranks == expected else "WARN"
    _add_check(
        checks,
        "candidate_rank_sequence",
        status,
        "candidate ranks are sequential" if status == "PASS" else "candidate ranks are not sequential",
        {"ranks": ranks, "expected": expected},
    )


def _audit_trade_dates(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]], snapshot_path: Path) -> None:
    missing_rows = [_row_identity(row, idx) for idx, row in enumerate(rows, start=1) if not _safe_text(row.get("trade_date"))]
    trade_dates = sorted(
        {
            _safe_text(row.get("trade_date"))
            for row in rows
            if _safe_text(row.get("trade_date"))
        }
    )
    if missing_rows:
        status = "FAIL"
        message = "snapshot rows are missing trade_date"
    else:
        status = "PASS"
        message = "all rows include trade_date"
    _add_check(
        checks,
        "trade_date_present",
        status,
        message,
        {"trade_dates": trade_dates, "missing_rows": missing_rows[:20]},
    )

    latest_trade_date = max(trade_dates) if trade_dates else ""
    expected_file = snapshot_path.parent / f"stock_ai_candidate_snapshot_{latest_trade_date.replace('-', '')}.jsonl"
    exists = bool(latest_trade_date and expected_file.exists())
    _add_check(
        checks,
        "dated_snapshot_latest_trade_date",
        "PASS" if exists else "FAIL",
        "dated snapshot file matches latest trade_date" if exists else "dated snapshot file for latest trade_date is missing",
        {"latest_trade_date": latest_trade_date, "expected_file": str(expected_file), "exists": exists},
    )


def _audit_required_dimensions(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    missing: List[Dict[str, Any]] = []
    dimension_status_counts: Dict[str, Dict[str, int]] = {}
    for idx, row in enumerate(rows, start=1):
        dimensions = _dimension_map(row)
        missing_names = sorted(REQUIRED_DIMENSIONS - set(dimensions))
        if missing_names:
            missing.append({"row": _row_identity(row, idx), "missing": missing_names})
        for name, dimension in dimensions.items():
            if name not in REQUIRED_DIMENSIONS:
                continue
            status = _safe_text(dimension.get("status")) or "missing"
            dimension_status_counts.setdefault(name, {})
            dimension_status_counts[name][status] = dimension_status_counts[name].get(status, 0) + 1
    _add_check(
        checks,
        "factor_snapshot_required_dimensions",
        "FAIL" if missing else "PASS",
        "required factor dimensions missing" if missing else "all required factor dimensions are present",
        {
            "required_dimensions": sorted(REQUIRED_DIMENSIONS),
            "missing": missing[:20],
            "dimension_status_counts": dimension_status_counts,
        },
    )


def _audit_data_coverage(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    labels = Counter()
    weak_rows: List[Dict[str, Any]] = []
    block_status_counts: Dict[str, Dict[str, int]] = {}
    weak_block_rows: List[Dict[str, Any]] = []
    critical_weak_block_rows: List[Dict[str, Any]] = []
    optional_weak_block_rows: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        dimension = _dimension_map(row).get("data_coverage", {})
        label = _safe_text(dimension.get("label")) or "missing"
        labels[label] += 1
        if label not in {"high", "medium"}:
            weak_rows.append({"row": _row_identity(row, idx), "label": label})

        coverage = row.get("data_coverage") if isinstance(row.get("data_coverage"), Mapping) else {}
        for block_name in EXPECTED_COVERAGE_BLOCKS:
            block = coverage.get(block_name) if isinstance(coverage, Mapping) else {}
            status = _safe_text(block.get("status")) if isinstance(block, Mapping) else ""
            status = status or "missing"
            block_status_counts.setdefault(block_name, {})
            block_status_counts[block_name][status] = block_status_counts[block_name].get(status, 0) + 1
            if status in GOOD_COVERAGE_STATUSES:
                continue
            weak_item = {
                "row": _row_identity(row, idx),
                "block": block_name,
                "status": status,
                "critical_block": block_name in CRITICAL_COVERAGE_BLOCKS,
                "missing_reasons": _safe_string_list(
                    block.get("missing_reasons") if isinstance(block, Mapping) else []
                ),
            }
            weak_block_rows.append(weak_item)
            if block_name in CRITICAL_COVERAGE_BLOCKS:
                critical_weak_block_rows.append(weak_item)
            else:
                optional_weak_block_rows.append(weak_item)
    _add_check(
        checks,
        "data_coverage_quality",
        "WARN" if weak_rows else "PASS",
        "some rows have weak data coverage" if weak_rows else "all rows have medium/high data coverage labels",
        {"label_counts": dict(labels), "weak_rows": weak_rows[:20]},
    )
    _add_check(
        checks,
        "data_coverage_block_status",
        "WARN" if critical_weak_block_rows else "PASS",
        "critical data coverage blocks are missing or weak"
        if critical_weak_block_rows
        else (
            "critical data coverage blocks are available or explicitly not supported; optional gaps are tracked"
            if optional_weak_block_rows
            else "critical data coverage blocks are available or explicitly not supported"
        ),
        {
            "expected_blocks": list(EXPECTED_COVERAGE_BLOCKS),
            "critical_blocks": sorted(CRITICAL_COVERAGE_BLOCKS),
            "block_status_counts": block_status_counts,
            "weak_block_rows": weak_block_rows[:30],
            "critical_weak_block_rows": critical_weak_block_rows[:30],
            "optional_weak_block_rows": optional_weak_block_rows[:30],
        },
    )


def _audit_low_sensitivity(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
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
        "low_sensitivity_boundary",
        status,
        "raw or sensitive payload markers found" if status == "FAIL" else "no raw or sensitive payload markers found",
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


def _dimension_map(row: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    factor_snapshot = row.get("factor_snapshot") if isinstance(row.get("factor_snapshot"), Mapping) else {}
    dimensions = factor_snapshot.get("dimensions") if isinstance(factor_snapshot, Mapping) else []
    if not isinstance(dimensions, list):
        return {}
    result: Dict[str, Mapping[str, Any]] = {}
    for item in dimensions:
        if not isinstance(item, Mapping):
            continue
        name = _safe_text(item.get("name"))
        if name:
            result[name] = item
    return result


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


def _row_identity(row: Mapping[str, Any], row_number: int) -> Dict[str, Any]:
    return {
        "row_number": row_number,
        "stock_code": _safe_text(row.get("stock_code")),
        "candidate_rank": row.get("candidate_rank"),
        "trade_date": _safe_text(row.get("trade_date")),
    }


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


def _audit_file_date(audit: Mapping[str, Any]) -> str:
    latest_trade_date = _safe_text(audit.get("latest_trade_date"))
    if latest_trade_date:
        return latest_trade_date.replace("-", "")
    generated_at = _safe_text(audit.get("generated_at"))
    if generated_at:
        return generated_at[:10].replace("-", "")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        text = _safe_text(item)
        if text and text not in result:
            result.append(text)
    return result[:10]


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_timestamp(value: Optional[datetime]) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit hosted AI candidate snapshot JSONL artifacts.")
    parser.add_argument("--snapshot-path", default=str(DEFAULT_SNAPSHOT_PATH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--no-fail", action="store_true", help="Return exit code 0 even when FAIL checks exist.")
    args = parser.parse_args(argv)

    audit = audit_snapshot(args.snapshot_path)
    paths = write_audit_outputs(audit, args.output_dir)
    print(
        "ai_snapshot_audit "
        f"status={audit['overall_status']} "
        f"rows={audit['row_count']} "
        f"checks={audit['status_counts']} "
        f"outputs={', '.join(str(path) for path in paths)}"
    )
    return 0 if args.no_fail or audit["overall_status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
