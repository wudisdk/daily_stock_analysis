# -*- coding: utf-8 -*-
"""Markdown Step Summary renderer for hosted AI snapshot artifacts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_INPUT_DIR = Path("reports") / "ai_snapshot"
AUDIT_FILES = (
    ("Snapshot audit", "stock_ai_candidate_snapshot_audit_latest.json"),
    ("Replay queue audit", "stock_ai_candidate_replay_queue_audit_latest.json"),
    ("Price history audit", "stock_ai_candidate_price_history_audit_latest.json"),
    ("Gap triage audit", "stock_ai_candidate_gap_triage_audit_latest.json"),
)


def build_step_summary(input_dir: Path | str = DEFAULT_INPUT_DIR) -> str:
    """Build a GitHub Step Summary section from AI snapshot audit artifacts."""
    root = Path(input_dir)
    audits = [(label, name, _load_json(root / name)) for label, name in AUDIT_FILES]

    lines: List[str] = [
        "",
        "### AI Snapshot Artifacts",
        "",
        "| Audit | Status | Rows | Checks |",
        "| --- | --- | ---: | --- |",
    ]
    for label, name, audit in audits:
        if audit is None:
            lines.append(f"| {label} | SKIPPED | 0 | missing `{name}` |")
            continue
        lines.append(
            "| "
            f"{label} | "
            f"{_safe_text(audit.get('overall_status')) or 'UNKNOWN'} | "
            f"{_int_or_zero(audit.get('row_count'))} | "
            f"{_format_status_counts(audit.get('status_counts'))} |"
        )

    lines.extend(_coverage_lines(_snapshot_audit(audits)))
    return "\n".join(lines).rstrip() + "\n"


def write_step_summary(
    input_dir: Path | str = DEFAULT_INPUT_DIR,
    output_file: Optional[Path | str] = None,
) -> str:
    """Build the summary and optionally write it to a Markdown file."""
    summary = build_step_summary(input_dir)
    if output_file:
        path = Path(output_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(summary, encoding="utf-8")
    return summary


def _coverage_lines(snapshot_audit: Optional[Mapping[str, Any]]) -> List[str]:
    lines = ["", "### AI Snapshot Data Coverage", ""]
    if not snapshot_audit:
        return [*lines, "- SKIPPED: missing snapshot audit artifact."]

    check = _check_by_id(snapshot_audit, "data_coverage_block_status")
    if not check:
        return [*lines, "- SKIPPED: snapshot audit has no `data_coverage_block_status` check."]

    evidence = check.get("evidence") if isinstance(check.get("evidence"), Mapping) else {}
    block_status_counts = evidence.get("block_status_counts")
    if not isinstance(block_status_counts, Mapping) or not block_status_counts:
        return [*lines, "- No data coverage block counts available yet."]

    critical_blocks = set(_safe_string_list(evidence.get("critical_blocks")))
    expected_blocks = _safe_string_list(evidence.get("expected_blocks")) or sorted(block_status_counts)
    lines.extend(
        [
            "| Block | Status Counts | Critical |",
            "| --- | --- | --- |",
        ]
    )
    for block in expected_blocks:
        counts = block_status_counts.get(block)
        if not isinstance(counts, Mapping):
            counts = {}
        lines.append(
            "| "
            f"{block} | "
            f"{_format_status_counts(counts)} | "
            f"{'yes' if block in critical_blocks else 'no'} |"
        )

    weak_rows = evidence.get("weak_block_rows")
    if not isinstance(weak_rows, list) or not weak_rows:
        lines.extend(["", "- Critical coverage blocks look usable."])
        return lines

    lines.extend(
        [
            "",
            "| Stock | Rank | Trade Date | Block | Status | Missing Reasons |",
            "| --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for item in weak_rows[:10]:
        item_map = item if isinstance(item, Mapping) else {}
        row = item_map.get("row") if isinstance(item_map.get("row"), Mapping) else {}
        lines.append(
            "| "
            f"{_safe_text(row.get('stock_code')) or '-'} | "
            f"{_safe_text(row.get('candidate_rank')) or '-'} | "
            f"{_safe_text(row.get('trade_date')) or '-'} | "
            f"{_safe_text(item_map.get('block')) or '-'} | "
            f"{_safe_text(item_map.get('status')) or '-'} | "
            f"{', '.join(_safe_string_list(item_map.get('missing_reasons'))) or '-'} |"
        )
    return lines


def _snapshot_audit(audits: Sequence[tuple[str, str, Optional[Mapping[str, Any]]]]) -> Optional[Mapping[str, Any]]:
    for label, _name, audit in audits:
        if label == "Snapshot audit":
            return audit
    return None


def _check_by_id(audit: Mapping[str, Any], check_id: str) -> Optional[Mapping[str, Any]]:
    checks = audit.get("checks")
    if not isinstance(checks, list):
        return None
    for check in checks:
        if isinstance(check, Mapping) and _safe_text(check.get("check_id")) == check_id:
            return check
    return None


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _format_status_counts(value: Any) -> str:
    if not isinstance(value, Mapping) or not value:
        return "-"
    parts = []
    for key in ("FAIL", "WARN", "PASS", "missing", "available", "not_supported"):
        if key in value:
            parts.append(f"{key}:{value[key]}")
    for key, count in sorted(
        (str(item), count)
        for item, count in value.items()
        if item not in {"FAIL", "WARN", "PASS", "missing", "available", "not_supported"}
    ):
        parts.append(f"{key}:{count}")
    return ", ".join(parts) if parts else "-"


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


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Render AI snapshot artifacts for GitHub Step Summary.")
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    parser.add_argument("--output-file")
    args = parser.parse_args(argv)

    summary = write_step_summary(args.input_dir, args.output_file)
    if not args.output_file:
        print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
