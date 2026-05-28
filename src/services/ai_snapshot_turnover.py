# -*- coding: utf-8 -*-
"""Compare hosted AI candidate snapshots for retention and turnover."""

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


TURNOVER_SCHEMA_VERSION = "1.0"
TURNOVER_AUDIT_SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = Path("reports") / "ai_snapshot"
DEFAULT_BUCKET_LIMITS = (5, 10, 20, 50)
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


def build_turnover_rows(
    base_rows: Sequence[Mapping[str, Any]],
    compare_rows: Sequence[Mapping[str, Any]],
    *,
    generated_at: Optional[datetime] = None,
    bucket_limits: Sequence[int] = DEFAULT_BUCKET_LIMITS,
) -> List[Dict[str, Any]]:
    """Build candidate retention and turnover rows for top-N snapshot buckets."""
    timestamp = _iso_timestamp(generated_at)
    base_codes_all = _ranked_unique_codes(base_rows)
    compare_codes_all = _ranked_unique_codes(compare_rows)
    buckets = _bucket_specs(len(base_codes_all), len(compare_codes_all), bucket_limits)
    base_trade_date = _latest_trade_date(base_rows)
    compare_trade_date = _latest_trade_date(compare_rows)

    rows: List[Dict[str, Any]] = []
    for bucket_name, limit in buckets:
        base_codes = base_codes_all if limit is None else base_codes_all[:limit]
        compare_codes = compare_codes_all if limit is None else compare_codes_all[:limit]
        base_set = set(base_codes)
        compare_set = set(compare_codes)
        retained = [code for code in base_codes if code in compare_set]
        added = [code for code in compare_codes if code not in base_set]
        removed = [code for code in base_codes if code not in compare_set]
        union = base_set | compare_set
        rows.append(
            _drop_none(
                {
                    "turnover_schema_version": TURNOVER_SCHEMA_VERSION,
                    "generated_at": timestamp,
                    "bucket": bucket_name,
                    "bucket_limit": limit,
                    "base_trade_date": base_trade_date,
                    "compare_trade_date": compare_trade_date,
                    "base_candidate_count": len(base_codes),
                    "compare_candidate_count": len(compare_codes),
                    "retained_count": len(retained),
                    "added_count": len(added),
                    "removed_count": len(removed),
                    "retention_rate": _rate(len(retained), len(base_codes)),
                    "candidate_turnover_rate": _rate(len(added) + len(removed), len(union)),
                    "jaccard_overlap_rate": _rate(len(base_set & compare_set), len(union)),
                    "retained_stock_codes": retained[:50],
                    "added_stock_codes": added[:50],
                    "removed_stock_codes": removed[:50],
                }
            )
        )
    return rows


def write_turnover_outputs(
    base_snapshot_path: Path | str,
    compare_snapshot_path: Path | str,
    *,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    generated_at: Optional[datetime] = None,
) -> List[Path]:
    """Write latest/dated turnover summary and audit artifacts."""
    base_path = Path(base_snapshot_path)
    compare_path = Path(compare_snapshot_path)
    base_rows = _load_jsonl(base_path)
    compare_rows = _load_jsonl(compare_path)
    rows = build_turnover_rows(base_rows, compare_rows, generated_at=generated_at)
    if not rows:
        return []

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_date = _turnover_file_date(rows, generated_at)

    latest_json = target_dir / "stock_ai_candidate_turnover_latest.json"
    dated_json = target_dir / f"stock_ai_candidate_turnover_{file_date}.json"
    latest_csv = target_dir / "stock_ai_candidate_turnover_latest.csv"
    dated_csv = target_dir / f"stock_ai_candidate_turnover_{file_date}.csv"

    _write_json(latest_json, rows)
    _write_json(dated_json, rows)
    _write_csv(latest_csv, rows)
    _write_csv(dated_csv, rows)

    audit = audit_turnover(
        base_rows,
        compare_rows,
        rows,
        base_snapshot_path=base_path,
        compare_snapshot_path=compare_path,
        output_dir=target_dir,
        generated_at=generated_at,
    )
    audit_paths = write_turnover_audit_outputs(audit, target_dir)
    return [latest_json, dated_json, latest_csv, dated_csv, *audit_paths]


def audit_turnover(
    base_rows: Sequence[Mapping[str, Any]],
    compare_rows: Sequence[Mapping[str, Any]],
    turnover_rows: Sequence[Mapping[str, Any]],
    *,
    base_snapshot_path: Path | str,
    compare_snapshot_path: Path | str,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Audit turnover summary coverage and low-sensitivity boundaries."""
    checks: List[Dict[str, Any]] = []
    _add_check(
        checks,
        "turnover_rows_non_empty",
        "PASS" if turnover_rows else "FAIL",
        f"turnover row count={len(turnover_rows)}",
        {
            "row_count": len(turnover_rows),
            "base_snapshot_count": len(base_rows),
            "compare_snapshot_count": len(compare_rows),
        },
    )
    _audit_snapshot_rows(checks, "base", base_rows)
    _audit_snapshot_rows(checks, "compare", compare_rows)
    _audit_turnover_buckets(checks, turnover_rows)
    _audit_dated_turnover_files(checks, turnover_rows, output_dir)
    _audit_low_sensitivity(checks, base_rows, compare_rows, turnover_rows)

    status_counts = dict(Counter(check["status"] for check in checks))
    overall_status = "FAIL" if status_counts.get("FAIL") else ("WARN" if status_counts.get("WARN") else "PASS")
    return {
        "audit_schema_version": TURNOVER_AUDIT_SCHEMA_VERSION,
        "generated_at": _iso_timestamp(generated_at),
        "overall_status": overall_status,
        "status_counts": status_counts,
        "row_count": len(turnover_rows),
        "base_snapshot_path": str(base_snapshot_path),
        "compare_snapshot_path": str(compare_snapshot_path),
        "base_trade_date": _latest_trade_date(base_rows),
        "compare_trade_date": _latest_trade_date(compare_rows),
        "checks": checks,
    }


def write_turnover_audit_outputs(
    audit: Mapping[str, Any],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> List[Path]:
    """Write latest and dated turnover audit JSON/CSV artifacts."""
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_date = _turnover_audit_file_date(audit)
    latest_json = target_dir / "stock_ai_candidate_turnover_audit_latest.json"
    dated_json = target_dir / f"stock_ai_candidate_turnover_audit_{file_date}.json"
    latest_csv = target_dir / "stock_ai_candidate_turnover_audit_latest.csv"
    dated_csv = target_dir / f"stock_ai_candidate_turnover_audit_{file_date}.csv"

    _write_json_value(latest_json, audit)
    _write_json_value(dated_json, audit)
    _write_checks_csv(latest_csv, audit.get("checks", []))
    _write_checks_csv(dated_csv, audit.get("checks", []))
    return [latest_json, dated_json, latest_csv, dated_csv]


def _audit_snapshot_rows(checks: List[Dict[str, Any]], label: str, rows: Sequence[Mapping[str, Any]]) -> None:
    missing_codes = []
    duplicate_codes = []
    codes = [_safe_text(row.get("stock_code")) for row in rows if _safe_text(row.get("stock_code"))]
    counts = Counter(codes)
    duplicate_codes = sorted(code for code, count in counts.items() if count > 1)
    for idx, row in enumerate(rows, start=1):
        if not _safe_text(row.get("stock_code")):
            missing_codes.append(idx)
    status = "FAIL" if not rows or missing_codes else ("WARN" if duplicate_codes else "PASS")
    _add_check(
        checks,
        f"{label}_snapshot_candidate_codes",
        status,
        f"{label} snapshot candidate codes checked",
        {
            "row_count": len(rows),
            "unique_stock_code_count": len(set(codes)),
            "missing_code_rows": missing_codes[:20],
            "duplicate_codes": duplicate_codes[:20],
        },
    )


def _audit_turnover_buckets(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    bucket_names = [_safe_text(row.get("bucket")) for row in rows]
    missing_all = "all" not in bucket_names
    invalid_rates = []
    for idx, row in enumerate(rows, start=1):
        for key in ("retention_rate", "candidate_turnover_rate", "jaccard_overlap_rate"):
            value = _numeric_or_none(row.get(key))
            if value is None or value < 0 or value > 1:
                invalid_rates.append({"row_number": idx, "bucket": _safe_text(row.get("bucket")), "field": key})
    status = "FAIL" if missing_all or invalid_rates else "PASS"
    _add_check(
        checks,
        "turnover_bucket_rates",
        status,
        "turnover bucket rates are valid" if status == "PASS" else "turnover bucket rates need review",
        {
            "buckets": bucket_names,
            "missing_all_bucket": missing_all,
            "invalid_rates": invalid_rates[:20],
        },
    )


def _audit_dated_turnover_files(
    checks: List[Dict[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    output_dir: Path | str,
) -> None:
    file_date = _turnover_file_date(rows, None)
    expected_json = Path(output_dir) / f"stock_ai_candidate_turnover_{file_date}.json"
    expected_csv = Path(output_dir) / f"stock_ai_candidate_turnover_{file_date}.csv"
    exists = expected_json.exists() and expected_csv.exists()
    _add_check(
        checks,
        "turnover_dated_files",
        "PASS" if exists else "FAIL",
        "dated turnover files are available" if exists else "dated turnover files are missing",
        {
            "expected_json": str(expected_json),
            "expected_csv": str(expected_csv),
            "json_exists": expected_json.exists(),
            "csv_exists": expected_csv.exists(),
        },
    )


def _audit_low_sensitivity(
    checks: List[Dict[str, Any]],
    base_rows: Sequence[Mapping[str, Any]],
    compare_rows: Sequence[Mapping[str, Any]],
    turnover_rows: Sequence[Mapping[str, Any]],
) -> None:
    payload = {"base": base_rows, "compare": compare_rows, "turnover": turnover_rows}
    key_hits: List[Dict[str, Any]] = []
    for key_path, key in _walk_keys(payload):
        if _normalize_key(key) in FORBIDDEN_RAW_KEYS:
            key_hits.append({"path": key_path, "key": key})
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    secret_hits = [pattern.pattern for pattern in SECRET_VALUE_PATTERNS if pattern.search(serialized)]
    status = "FAIL" if key_hits or secret_hits else "PASS"
    _add_check(
        checks,
        "turnover_low_sensitivity_boundary",
        status,
        "raw or sensitive payload markers found" if status == "FAIL" else "no raw or sensitive payload markers found",
        {"key_hits": key_hits[:20], "secret_value_patterns": secret_hits},
    )


def _ranked_unique_codes(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    sorted_rows = sorted(
        rows,
        key=lambda row: (_int_or_none(row.get("candidate_rank")) or 999999, _safe_text(row.get("stock_code"))),
    )
    codes: List[str] = []
    seen: set[str] = set()
    for row in sorted_rows:
        code = _safe_text(row.get("stock_code"))
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    return codes


def _bucket_specs(base_count: int, compare_count: int, bucket_limits: Sequence[int]) -> List[tuple[str, Optional[int]]]:
    max_count = max(base_count, compare_count)
    buckets: List[tuple[str, Optional[int]]] = []
    for limit in sorted({_int_or_none(value) or 0 for value in bucket_limits}):
        if limit > 0 and max_count >= limit:
            buckets.append((f"top_{limit}", limit))
    buckets.append(("all", None))
    return buckets


def _latest_trade_date(rows: Sequence[Mapping[str, Any]]) -> str:
    dates = sorted({_safe_text(row.get("trade_date")) for row in rows if _safe_text(row.get("trade_date"))})
    return dates[-1] if dates else ""


def _rate(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


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


def _write_json(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(json.dumps(list(rows), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_json_value(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _turnover_file_date(rows: Sequence[Mapping[str, Any]], generated_at: Optional[datetime]) -> str:
    dates = [_safe_text(row.get("compare_trade_date")) for row in rows if _safe_text(row.get("compare_trade_date"))]
    if dates:
        return max(dates).replace("-", "")
    return _iso_timestamp(generated_at)[:10].replace("-", "")


def _turnover_audit_file_date(audit: Mapping[str, Any]) -> str:
    compare_trade_date = _safe_text(audit.get("compare_trade_date"))
    if compare_trade_date:
        return compare_trade_date.replace("-", "")
    generated_at = _safe_text(audit.get("generated_at"))
    if generated_at:
        return generated_at[:10].replace("-", "")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


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


def _drop_none(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if value is not None}


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
    parser = argparse.ArgumentParser(description="Compare AI candidate snapshots for retention and turnover.")
    parser.add_argument("--base-snapshot-path", required=True)
    parser.add_argument("--compare-snapshot-path", required=True)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--no-fail", action="store_true", help="Return exit code 0 even when turnover audit fails.")
    args = parser.parse_args(argv)

    paths = write_turnover_outputs(
        args.base_snapshot_path,
        args.compare_snapshot_path,
        output_dir=args.output_dir,
    )
    if not paths:
        print("ai_snapshot_turnover rows=0 outputs=")
        return 1
    audit_path = Path(args.output_dir) / "stock_ai_candidate_turnover_audit_latest.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8-sig"))
    rows = json.loads((Path(args.output_dir) / "stock_ai_candidate_turnover_latest.json").read_text(encoding="utf-8-sig"))
    print(
        "ai_snapshot_turnover "
        f"rows={len(rows)} "
        f"audit_status={audit['overall_status']} "
        f"outputs={', '.join(str(path) for path in paths)}"
    )
    return 0 if args.no_fail or audit["overall_status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
