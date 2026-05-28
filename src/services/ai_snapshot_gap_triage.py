# -*- coding: utf-8 -*-
"""Triage low-sensitivity data coverage gaps from AI snapshot audits."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


GAP_TRIAGE_SCHEMA_VERSION = "1.0"
GAP_TRIAGE_AUDIT_SCHEMA_VERSION = "1.0"
DEFAULT_AUDIT_PATH = Path("reports") / "ai_snapshot" / "stock_ai_candidate_snapshot_audit_latest.json"
DEFAULT_OUTPUT_DIR = Path("reports") / "ai_snapshot"
CRITICAL_BLOCKS = {"quote", "daily_bars", "technical", "factor_snapshot"}
REQUIRED_DETAIL_FIELDS = {
    "gap_schema_version",
    "generated_at",
    "gap_id",
    "block",
    "missing_reason",
    "critical_block",
    "severity",
    "action_bucket",
    "affected_row_count",
}
REQUIRED_SUMMARY_FIELDS = {
    "gap_schema_version",
    "generated_at",
    "severity",
    "action_bucket",
    "action_state",
    "gap_count",
    "affected_row_count",
    "critical_gap_count",
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
ACTION_BUCKETS = {
    "quote": (
        "realtime_quote_source_gap",
        "Check realtime provider priority, provider API credentials, and quote fallback labeling.",
    ),
    "daily_bars": (
        "daily_bar_history_gap",
        "Check local daily price database freshness, market calendar date, and code alias joins.",
    ),
    "technical": (
        "technical_context_gap",
        "Check trend-analysis input wiring and daily bar availability before AI prompt construction.",
    ),
    "factor_snapshot": (
        "factor_snapshot_gap",
        "Check factor snapshot builder inputs and whether upstream quote/daily/fundamental blocks are missing.",
    ),
    "fundamentals": (
        "fundamental_context_gap",
        "Check Tushare/yfinance financial context coverage and point-in-time fundamental fetchers.",
    ),
    "news": (
        "news_context_gap",
        "Check search-provider quota, cooldown markers, and news retrieval fallback order.",
    ),
}


def build_gap_triage_rows(snapshot_audit: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Aggregate weak coverage rows into prioritized triage rows."""
    check = _check_by_id(snapshot_audit, "data_coverage_block_status")
    if not check:
        return []
    evidence = check.get("evidence") if isinstance(check.get("evidence"), Mapping) else {}
    weak_rows = evidence.get("weak_block_rows")
    if not isinstance(weak_rows, list):
        return []

    generated_at = _safe_text(snapshot_audit.get("generated_at")) or _iso_timestamp(None)
    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for item in weak_rows:
        item_map = item if isinstance(item, Mapping) else {}
        block = _safe_text(item_map.get("block")) or "unknown"
        missing_reasons = _safe_string_list(item_map.get("missing_reasons")) or ["unknown"]
        for reason in missing_reasons:
            key = (block, reason)
            bucket = grouped.setdefault(
                key,
                {
                    "gap_schema_version": GAP_TRIAGE_SCHEMA_VERSION,
                    "generated_at": generated_at,
                    "gap_id": f"{block}:{reason}",
                    "block": block,
                    "missing_reason": reason,
                    "critical_block": block in CRITICAL_BLOCKS,
                    "severity": "WARN" if block in CRITICAL_BLOCKS else "INFO",
                    "action_bucket": _action_bucket(block)[0],
                    "suggested_next_step": _action_bucket(block)[1],
                    "affected_row_count": 0,
                    "affected_stock_codes": [],
                    "affected_candidate_ranks": [],
                    "trade_dates": [],
                },
            )
            bucket["affected_row_count"] += 1
            row = item_map.get("row") if isinstance(item_map.get("row"), Mapping) else {}
            _append_unique(bucket["affected_stock_codes"], _safe_text(row.get("stock_code")), limit=50)
            _append_unique(bucket["affected_candidate_ranks"], _safe_text(row.get("candidate_rank")), limit=50)
            _append_unique(bucket["trade_dates"], _safe_text(row.get("trade_date")), limit=20)

    rows = list(grouped.values())
    rows.sort(
        key=lambda row: (
            0 if row["severity"] == "WARN" else 1,
            -int(row["affected_row_count"]),
            row["block"],
            row["missing_reason"],
        )
    )
    return rows


def build_gap_triage_summary_rows(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Summarize triage rows by severity and action bucket."""
    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        severity = _safe_text(row.get("severity")) or "INFO"
        action_bucket = _safe_text(row.get("action_bucket")) or "unclassified_data_gap"
        key = (severity, action_bucket)
        bucket = grouped.setdefault(
            key,
            {
                "gap_schema_version": GAP_TRIAGE_SCHEMA_VERSION,
                "generated_at": _safe_text(row.get("generated_at")) or _iso_timestamp(None),
                "severity": severity,
                "action_bucket": action_bucket,
                "action_state": _action_state(severity),
                "gap_count": 0,
                "affected_row_count": 0,
                "critical_gap_count": 0,
                "blocks": [],
                "missing_reasons": [],
                "suggested_next_step": _safe_text(row.get("suggested_next_step")),
            },
        )
        bucket["gap_count"] += 1
        bucket["affected_row_count"] += _int_or_zero(row.get("affected_row_count"))
        if bool(row.get("critical_block")):
            bucket["critical_gap_count"] += 1
        _append_unique(bucket["blocks"], _safe_text(row.get("block")), limit=20)
        _append_unique(bucket["missing_reasons"], _safe_text(row.get("missing_reason")), limit=50)

    summary = list(grouped.values())
    summary.sort(
        key=lambda row: (
            0 if row["severity"] == "WARN" else 1,
            -int(row["affected_row_count"]),
            row["action_bucket"],
        )
    )
    return summary


def audit_gap_triage(
    rows: Sequence[Mapping[str, Any]],
    summary_rows: Sequence[Mapping[str, Any]],
    *,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build PASS/WARN/FAIL checks for gap triage detail and summary artifacts."""
    checks: List[Dict[str, Any]] = []
    _add_check(
        checks,
        "gap_triage_detail_schema",
        "FAIL" if _missing_required_fields(rows, REQUIRED_DETAIL_FIELDS) else "PASS",
        "gap detail rows have required fields"
        if not _missing_required_fields(rows, REQUIRED_DETAIL_FIELDS)
        else "gap detail rows are missing required fields",
        {"missing": _missing_required_fields(rows, REQUIRED_DETAIL_FIELDS)[:20], "row_count": len(rows)},
    )
    _add_check(
        checks,
        "gap_triage_summary_schema",
        "FAIL" if _missing_required_fields(summary_rows, REQUIRED_SUMMARY_FIELDS) else "PASS",
        "gap summary rows have required fields"
        if not _missing_required_fields(summary_rows, REQUIRED_SUMMARY_FIELDS)
        else "gap summary rows are missing required fields",
        {"missing": _missing_required_fields(summary_rows, REQUIRED_SUMMARY_FIELDS)[:20], "row_count": len(summary_rows)},
    )

    mismatches = _summary_mismatches(rows, summary_rows)
    _add_check(
        checks,
        "gap_triage_summary_consistency",
        "FAIL" if mismatches else "PASS",
        "gap summary totals match detail rows" if not mismatches else "gap summary totals do not match detail rows",
        {"mismatches": mismatches[:20]},
    )

    severity_counts = _severity_counts(rows)
    critical_gap_count = sum(1 for row in rows if bool(row.get("critical_block")))
    _add_check(
        checks,
        "gap_triage_critical_gap_visibility",
        "WARN" if critical_gap_count else "PASS",
        "critical data-source gaps are visible"
        if critical_gap_count
        else "no critical data-source gaps are visible",
        {"severity_counts": severity_counts, "critical_gap_count": critical_gap_count},
    )
    optional_gap_count = sum(1 for row in rows if not bool(row.get("critical_block")))
    _add_check(
        checks,
        "gap_triage_optional_gap_visibility",
        "PASS",
        "optional source debt is tracked" if optional_gap_count else "no optional source debt is visible",
        {"optional_gap_count": optional_gap_count},
    )

    sensitivity_hits = _low_sensitivity_hits([*rows, *summary_rows])
    _add_check(
        checks,
        "gap_triage_low_sensitivity_boundary",
        "FAIL" if sensitivity_hits["key_hits"] or sensitivity_hits["secret_value_patterns"] else "PASS",
        "raw or sensitive payload markers found"
        if sensitivity_hits["key_hits"] or sensitivity_hits["secret_value_patterns"]
        else "no raw or sensitive payload markers found",
        sensitivity_hits,
    )

    status_counts = _severity_counts(checks, key="status")
    overall_status = "FAIL" if status_counts.get("FAIL") else ("WARN" if status_counts.get("WARN") else "PASS")
    return {
        "audit_schema_version": GAP_TRIAGE_AUDIT_SCHEMA_VERSION,
        "generated_at": _iso_timestamp(generated_at),
        "overall_status": overall_status,
        "status_counts": status_counts,
        "row_count": len(rows),
        "summary_row_count": len(summary_rows),
        "checks": checks,
    }


def write_gap_triage_outputs(
    snapshot_audit_path: Path | str = DEFAULT_AUDIT_PATH,
    *,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> List[Path]:
    """Write latest and dated gap triage JSON/CSV/Markdown artifacts."""
    audit_path = Path(snapshot_audit_path)
    audit = _load_json(audit_path)
    rows = build_gap_triage_rows(audit) if audit else []
    summary_rows = build_gap_triage_summary_rows(rows)
    triage_audit = audit_gap_triage(rows, summary_rows)
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_date = _triage_file_date(audit)

    latest_json = target_dir / "stock_ai_candidate_gap_triage_latest.json"
    dated_json = target_dir / f"stock_ai_candidate_gap_triage_{file_date}.json"
    latest_csv = target_dir / "stock_ai_candidate_gap_triage_latest.csv"
    dated_csv = target_dir / f"stock_ai_candidate_gap_triage_{file_date}.csv"
    latest_summary_json = target_dir / "stock_ai_candidate_gap_triage_summary_latest.json"
    dated_summary_json = target_dir / f"stock_ai_candidate_gap_triage_summary_{file_date}.json"
    latest_summary_csv = target_dir / "stock_ai_candidate_gap_triage_summary_latest.csv"
    dated_summary_csv = target_dir / f"stock_ai_candidate_gap_triage_summary_{file_date}.csv"
    latest_audit_json = target_dir / "stock_ai_candidate_gap_triage_audit_latest.json"
    dated_audit_json = target_dir / f"stock_ai_candidate_gap_triage_audit_{file_date}.json"
    latest_audit_csv = target_dir / "stock_ai_candidate_gap_triage_audit_latest.csv"
    dated_audit_csv = target_dir / f"stock_ai_candidate_gap_triage_audit_{file_date}.csv"
    latest_md = target_dir / "stock_ai_candidate_gap_triage_latest.md"
    dated_md = target_dir / f"stock_ai_candidate_gap_triage_{file_date}.md"

    _write_json(latest_json, rows)
    _write_json(dated_json, rows)
    _write_csv(latest_csv, rows)
    _write_csv(dated_csv, rows)
    _write_json(latest_summary_json, summary_rows)
    _write_json(dated_summary_json, summary_rows)
    _write_summary_csv(latest_summary_csv, summary_rows)
    _write_summary_csv(dated_summary_csv, summary_rows)
    _write_audit_json(latest_audit_json, triage_audit)
    _write_audit_json(dated_audit_json, triage_audit)
    _write_audit_csv(latest_audit_csv, triage_audit.get("checks", []))
    _write_audit_csv(dated_audit_csv, triage_audit.get("checks", []))
    rendered = render_gap_triage_markdown(
        rows,
        summary_rows=summary_rows,
        triage_audit=triage_audit,
        audit_path=audit_path,
        audit=audit,
    )
    latest_md.write_text(rendered, encoding="utf-8")
    dated_md.write_text(rendered, encoding="utf-8")
    return [
        latest_json,
        dated_json,
        latest_csv,
        dated_csv,
        latest_summary_json,
        dated_summary_json,
        latest_summary_csv,
        dated_summary_csv,
        latest_audit_json,
        dated_audit_json,
        latest_audit_csv,
        dated_audit_csv,
        latest_md,
        dated_md,
    ]


def render_gap_triage_markdown(
    rows: Sequence[Mapping[str, Any]],
    *,
    summary_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    triage_audit: Optional[Mapping[str, Any]] = None,
    audit_path: Path | str = DEFAULT_AUDIT_PATH,
    audit: Optional[Mapping[str, Any]] = None,
) -> str:
    """Render gap triage rows for GitHub Step Summary."""
    lines = [
        "",
        "### AI Snapshot Data Gap Triage",
        "",
    ]
    if audit is None:
        lines.append(f"- SKIPPED: missing snapshot audit `{Path(audit_path).name}`.")
        return "\n".join(lines).rstrip() + "\n"
    if not rows:
        lines.append("- No weak data coverage gaps found.")
        return "\n".join(lines).rstrip() + "\n"

    if triage_audit:
        lines.extend(
            [
                "| Audit | Status | Detail Rows | Summary Rows | Checks |",
                "| --- | --- | ---: | ---: | --- |",
                "| "
                "Gap triage self-audit | "
                f"{_safe_text(triage_audit.get('overall_status')) or 'UNKNOWN'} | "
                f"{_safe_text(triage_audit.get('row_count')) or '0'} | "
                f"{_safe_text(triage_audit.get('summary_row_count')) or '0'} | "
                f"{_format_status_counts(triage_audit.get('status_counts'))} |",
                "",
            ]
        )

    summary = list(summary_rows) if summary_rows is not None else build_gap_triage_summary_rows(rows)
    if summary:
        lines.extend(
            [
                "| Severity | Action Bucket | Gaps | Affected Rows | Blocks | Action State |",
                "| --- | --- | ---: | ---: | --- | --- |",
            ]
        )
        for row in summary[:8]:
            lines.append(
                "| "
                f"{_safe_text(row.get('severity'))} | "
                f"{_safe_text(row.get('action_bucket'))} | "
                f"{_safe_text(row.get('gap_count')) or '0'} | "
                f"{_safe_text(row.get('affected_row_count')) or '0'} | "
                f"{', '.join(_safe_string_list(row.get('blocks'))) or '-'} | "
                f"{_safe_text(row.get('action_state'))} |"
            )
        lines.append("")

    lines.extend(
        [
            "| Severity | Block | Missing Reason | Rows | Action Bucket | Next Step |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for row in rows[:12]:
        lines.append(
            "| "
            f"{_safe_text(row.get('severity'))} | "
            f"{_safe_text(row.get('block'))} | "
            f"{_safe_text(row.get('missing_reason'))} | "
            f"{_safe_text(row.get('affected_row_count')) or '0'} | "
            f"{_safe_text(row.get('action_bucket'))} | "
            f"{_safe_text(row.get('suggested_next_step'))} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def _check_by_id(audit: Mapping[str, Any], check_id: str) -> Optional[Mapping[str, Any]]:
    checks = audit.get("checks")
    if not isinstance(checks, list):
        return None
    for check in checks:
        if isinstance(check, Mapping) and _safe_text(check.get("check_id")) == check_id:
            return check
    return None


def _action_bucket(block: str) -> tuple[str, str]:
    return ACTION_BUCKETS.get(
        block,
        (
            "unclassified_data_gap",
            "Review the snapshot audit evidence and map this missing reason to a concrete data source.",
        ),
    )


def _append_unique(values: List[str], value: str, *, limit: int) -> None:
    if value and value not in values and len(values) < limit:
        values.append(value)


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _write_json(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(json.dumps(list(rows), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_audit_json(path: Path, audit: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(dict(audit), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = [
        "gap_schema_version",
        "generated_at",
        "gap_id",
        "block",
        "missing_reason",
        "critical_block",
        "severity",
        "action_bucket",
        "suggested_next_step",
        "affected_row_count",
        "affected_stock_codes",
        "affected_candidate_ranks",
        "trade_dates",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _write_audit_csv(path: Path, checks: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = ["check_id", "status", "message", "evidence"]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for check in checks:
            writer.writerow(
                {
                    "check_id": _safe_text(check.get("check_id")),
                    "status": _safe_text(check.get("status")),
                    "message": _safe_text(check.get("message")),
                    "evidence": _csv_value(check.get("evidence")),
                }
            )


def _write_summary_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = [
        "gap_schema_version",
        "generated_at",
        "severity",
        "action_bucket",
        "action_state",
        "gap_count",
        "affected_row_count",
        "critical_gap_count",
        "blocks",
        "missing_reasons",
        "suggested_next_step",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _format_status_counts(value: Any) -> str:
    if not isinstance(value, Mapping) or not value:
        return "-"
    parts: List[str] = []
    for key in ("FAIL", "WARN", "PASS"):
        if key in value:
            parts.append(f"{key}:{value[key]}")
    for key, count in sorted((str(item), count) for item, count in value.items() if item not in {"FAIL", "WARN", "PASS"}):
        parts.append(f"{key}:{count}")
    return ", ".join(parts) if parts else "-"


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


def _missing_required_fields(rows: Sequence[Mapping[str, Any]], required_fields: set[str]) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        missing_fields = sorted(field for field in required_fields if _safe_text(row.get(field)) == "")
        if missing_fields:
            missing.append({"row": idx, "missing_fields": missing_fields})
    return missing


def _summary_mismatches(
    rows: Sequence[Mapping[str, Any]],
    summary_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    expected: Dict[tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: {"gap_count": 0, "affected_row_count": 0, "critical_gap_count": 0}
    )
    for row in rows:
        key = (
            _safe_text(row.get("severity")) or "INFO",
            _safe_text(row.get("action_bucket")) or "unclassified_data_gap",
        )
        expected[key]["gap_count"] += 1
        expected[key]["affected_row_count"] += _int_or_zero(row.get("affected_row_count"))
        if bool(row.get("critical_block")):
            expected[key]["critical_gap_count"] += 1

    observed: Dict[tuple[str, str], Dict[str, int]] = {}
    for row in summary_rows:
        key = (
            _safe_text(row.get("severity")) or "INFO",
            _safe_text(row.get("action_bucket")) or "unclassified_data_gap",
        )
        observed[key] = {
            "gap_count": _int_or_zero(row.get("gap_count")),
            "affected_row_count": _int_or_zero(row.get("affected_row_count")),
            "critical_gap_count": _int_or_zero(row.get("critical_gap_count")),
        }

    mismatches: List[Dict[str, Any]] = []
    for key in sorted(set(expected) | set(observed)):
        if expected.get(key) != observed.get(key):
            mismatches.append(
                {
                    "severity": key[0],
                    "action_bucket": key[1],
                    "expected": expected.get(key),
                    "observed": observed.get(key),
                }
            )
    return mismatches


def _severity_counts(rows: Sequence[Mapping[str, Any]], *, key: str = "severity") -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        value = _safe_text(row.get(key)) or "UNKNOWN"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _low_sensitivity_hits(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    key_hits: List[Dict[str, Any]] = []
    for row_idx, row in enumerate(rows, start=1):
        for key_path, key in _walk_keys(row):
            normalized = _normalize_key(key)
            if normalized in FORBIDDEN_RAW_KEYS:
                key_hits.append({"row": row_idx, "path": key_path, "key": key})

    serialized = json.dumps(list(rows), ensure_ascii=False, sort_keys=True)
    secret_hits = [pattern.pattern for pattern in SECRET_VALUE_PATTERNS if pattern.search(serialized)]
    return {"key_hits": key_hits[:20], "secret_value_patterns": secret_hits}


def _walk_keys(value: Any, prefix: str = "") -> List[tuple[str, str]]:
    found: List[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            found.append((path, key_text))
            found.extend(_walk_keys(item, path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(_walk_keys(item, f"{prefix}[{idx}]"))
    return found


def _normalize_key(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _action_state(severity: str) -> str:
    return "review_or_fix_before_trusting_score" if severity == "WARN" else "track_optional_source_debt"


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _triage_file_date(audit: Optional[Mapping[str, Any]]) -> str:
    if audit:
        latest_trade_date = _safe_text(audit.get("latest_trade_date"))
        if latest_trade_date:
            return latest_trade_date.replace("-", "")
        generated_at = _safe_text(audit.get("generated_at"))
        if generated_at:
            return generated_at[:10].replace("-", "")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _safe_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        text = _safe_text(item)
        if text and text not in result:
            result.append(text)
    return result[:20]


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _iso_timestamp(value: Optional[datetime]) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Triage AI snapshot data coverage gaps.")
    parser.add_argument("--snapshot-audit-path", default=str(DEFAULT_AUDIT_PATH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args(argv)

    paths = write_gap_triage_outputs(args.snapshot_audit_path, output_dir=args.output_dir)
    latest_json = Path(args.output_dir) / "stock_ai_candidate_gap_triage_latest.json"
    rows = json.loads(latest_json.read_text(encoding="utf-8-sig")) if latest_json.exists() else []
    print(f"ai_snapshot_gap_triage rows={len(rows)} outputs={', '.join(str(path) for path in paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
