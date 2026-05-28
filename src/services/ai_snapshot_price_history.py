# -*- coding: utf-8 -*-
"""Export low-sensitivity price history for AI snapshot replay validation."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


PRICE_HISTORY_SCHEMA_VERSION = "1.0"
PRICE_HISTORY_AUDIT_SCHEMA_VERSION = "1.0"
DEFAULT_SNAPSHOT_PATH = Path("reports") / "ai_snapshot" / "stock_ai_candidate_snapshot_latest.jsonl"
DEFAULT_QUEUE_PATH = Path("reports") / "ai_snapshot" / "stock_ai_candidate_replay_queue_latest.jsonl"
DEFAULT_DATABASE_PATH = Path("data") / "stock_analysis.db"
DEFAULT_OUTPUT_DIR = Path("reports") / "ai_snapshot"
REQUIRED_PRICE_COLUMNS = {
    "price_history_schema_version",
    "stock_code",
    "trade_date",
    "close",
}
FORBIDDEN_RAW_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "news_content",
    "password",
    "secret",
    "token",
}
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
)


def load_candidate_specs(
    *,
    snapshot_path: Path | str | None = DEFAULT_SNAPSHOT_PATH,
    queue_path: Path | str | None = DEFAULT_QUEUE_PATH,
) -> List[Dict[str, Any]]:
    """Load candidate stock/date specs from snapshot and replay queue JSONL files."""
    candidates: Dict[str, Dict[str, Any]] = {}
    for row in _load_optional_jsonl(snapshot_path):
        _merge_candidate_row(candidates, row, forward_days=0)
    for row in _load_optional_jsonl(queue_path):
        _merge_candidate_row(
            candidates,
            row,
            forward_days=_int_or_none(row.get("forward_trading_days")) or 0,
        )

    result = []
    for candidate in candidates.values():
        anchors = candidate.pop("_anchors", {})
        candidate["anchor_dates"] = [
            {"trade_date": trade_date, "max_forward_trading_days": anchors[trade_date]}
            for trade_date in sorted(anchors)
        ]
        result.append(candidate)
    return sorted(result, key=lambda item: item["stock_code"])


def build_price_history_rows(
    candidates: Sequence[Mapping[str, Any]],
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    scope: str = "candidates",
    generated_at: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Read matching stock_daily close prices from SQLite and return canonical rows."""
    timestamp = _iso_timestamp(generated_at)
    db_path = Path(database_path)
    include_database_scope = _normalize_scope(scope) == "database"
    if (not candidates and not include_database_scope) or not db_path.exists():
        return []

    alias_to_candidates: Dict[str, List[Mapping[str, Any]]] = {}
    for candidate in candidates:
        for alias in _stock_code_aliases(_safe_text(candidate.get("stock_code"))):
            alias_to_candidates.setdefault(alias, []).append(candidate)

    if not alias_to_candidates and not include_database_scope:
        return []

    selected: Dict[tuple[str, str], Dict[str, Any]] = {}
    db_uri = db_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as con:
        if not _sqlite_table_exists(con, "stock_daily"):
            return []
        if include_database_scope:
            rows_iter = con.execute(
                "SELECT code, date, close, data_source "
                "FROM stock_daily "
                "WHERE close IS NOT NULL "
                "ORDER BY code, date"
            )
            for db_code, trade_date, close, data_source in rows_iter:
                _merge_price_row(
                    selected,
                    alias_to_candidates=alias_to_candidates,
                    include_database_scope=True,
                    timestamp=timestamp,
                    db_code=db_code,
                    trade_date=trade_date,
                    close=close,
                    data_source=data_source,
                )
        else:
            for aliases in _chunks(sorted(alias_to_candidates), 400):
                placeholders = ",".join("?" for _ in aliases)
                query = (
                    "SELECT code, date, close, data_source "
                    "FROM stock_daily "
                    f"WHERE code IN ({placeholders}) AND close IS NOT NULL "
                    "ORDER BY code, date"
                )
                for db_code, trade_date, close, data_source in con.execute(query, aliases):
                    _merge_price_row(
                        selected,
                        alias_to_candidates=alias_to_candidates,
                        include_database_scope=False,
                        timestamp=timestamp,
                        db_code=db_code,
                        trade_date=trade_date,
                        close=close,
                        data_source=data_source,
                    )

    return [selected[key] for key in sorted(selected)]


def write_price_history_outputs(
    *,
    snapshot_path: Path | str | None = DEFAULT_SNAPSHOT_PATH,
    queue_path: Path | str | None = DEFAULT_QUEUE_PATH,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    scope: str = "candidates",
    generated_at: Optional[datetime] = None,
) -> List[Path]:
    """Write latest and dated JSONL/CSV price-history artifacts."""
    candidates = load_candidate_specs(snapshot_path=snapshot_path, queue_path=queue_path)
    rows = build_price_history_rows(
        candidates,
        database_path=database_path,
        scope=scope,
        generated_at=generated_at,
    )
    if not rows:
        return []

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    history_date = _price_history_file_date(rows, candidates, generated_at)

    latest_jsonl = target_dir / "stock_ai_candidate_price_history_latest.jsonl"
    dated_jsonl = target_dir / f"stock_ai_candidate_price_history_{history_date}.jsonl"
    latest_csv = target_dir / "stock_ai_candidate_price_history_latest.csv"
    dated_csv = target_dir / f"stock_ai_candidate_price_history_{history_date}.csv"

    _write_jsonl(latest_jsonl, rows)
    _write_jsonl(dated_jsonl, rows)
    _write_csv(latest_csv, rows)
    _write_csv(dated_csv, rows)
    return [latest_jsonl, dated_jsonl, latest_csv, dated_csv]


def audit_price_history(
    candidates: Sequence[Mapping[str, Any]],
    price_rows: Sequence[Mapping[str, Any]],
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    scope: str = "candidates",
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Audit price-history coverage and low-sensitivity boundaries."""
    checks: List[Dict[str, Any]] = []
    candidate_codes = {
        _safe_text(candidate.get("stock_code"))
        for candidate in candidates
        if candidate.get("stock_code")
    }
    row_codes = {_safe_text(row.get("stock_code")) for row in price_rows if row.get("stock_code")}
    by_code_date = {
        (_safe_text(row.get("stock_code")), _safe_text(row.get("trade_date")))
        for row in price_rows
        if _safe_text(row.get("stock_code")) and _safe_text(row.get("trade_date"))
    }

    _add_check(
        checks,
        "price_history_rows_non_empty",
        "PASS" if price_rows else ("WARN" if not candidates else "FAIL"),
        f"price history row count={len(price_rows)}",
        {"row_count": len(price_rows), "candidate_count": len(candidate_codes)},
    )
    _audit_required_columns(checks, price_rows)
    _audit_candidate_coverage(checks, candidate_codes, row_codes)
    _audit_anchor_coverage(checks, candidates, by_code_date)
    _audit_forward_window_coverage(checks, candidates, price_rows)
    _audit_low_sensitivity(checks, price_rows)

    status_counts = dict(Counter(check["status"] for check in checks))
    overall_status = "FAIL" if status_counts.get("FAIL") else ("WARN" if status_counts.get("WARN") else "PASS")
    trade_dates = sorted({_safe_text(row.get("trade_date")) for row in price_rows if _safe_text(row.get("trade_date"))})
    return {
        "audit_schema_version": PRICE_HISTORY_AUDIT_SCHEMA_VERSION,
        "generated_at": _iso_timestamp(generated_at),
        "database_path": str(database_path),
        "scope": _normalize_scope(scope),
        "overall_status": overall_status,
        "status_counts": status_counts,
        "row_count": len(price_rows),
        "candidate_count": len(candidate_codes),
        "covered_candidate_count": len(candidate_codes & row_codes),
        "trade_dates": trade_dates,
        "latest_trade_date": max(trade_dates) if trade_dates else None,
        "checks": checks,
    }


def write_price_history_audit_outputs(
    audit: Mapping[str, Any],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> List[Path]:
    """Write latest and dated price-history audit JSON/CSV artifacts."""
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    audit_date = _audit_file_date(audit)

    latest_json = target_dir / "stock_ai_candidate_price_history_audit_latest.json"
    dated_json = target_dir / f"stock_ai_candidate_price_history_audit_{audit_date}.json"
    latest_csv = target_dir / "stock_ai_candidate_price_history_audit_latest.csv"
    dated_csv = target_dir / f"stock_ai_candidate_price_history_audit_{audit_date}.csv"

    payload = json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True)
    latest_json.write_text(payload + "\n", encoding="utf-8")
    dated_json.write_text(payload + "\n", encoding="utf-8")
    _write_checks_csv(latest_csv, audit.get("checks", []))
    _write_checks_csv(dated_csv, audit.get("checks", []))
    return [latest_json, dated_json, latest_csv, dated_csv]


def _merge_candidate_row(
    candidates: Dict[str, Dict[str, Any]],
    row: Mapping[str, Any],
    *,
    forward_days: int,
) -> None:
    stock_code = _safe_text(row.get("stock_code"))
    if not stock_code:
        return
    candidate = candidates.setdefault(
        stock_code,
        {
            "stock_code": stock_code,
            "stock_name": _safe_text(row.get("stock_name")),
            "_anchors": {},
        },
    )
    if not candidate.get("stock_name"):
        candidate["stock_name"] = _safe_text(row.get("stock_name"))
    trade_date = _normalize_trade_date(row.get("trade_date"))
    if trade_date:
        anchors = candidate.setdefault("_anchors", {})
        anchors[trade_date] = max(int(forward_days or 0), int(anchors.get(trade_date, 0)))


def _merge_price_row(
    selected: Dict[tuple[str, str], Dict[str, Any]],
    *,
    alias_to_candidates: Mapping[str, Sequence[Mapping[str, Any]]],
    include_database_scope: bool,
    timestamp: str,
    db_code: Any,
    trade_date: Any,
    close: Any,
    data_source: Any,
) -> None:
    db_code_text = _safe_text(db_code)
    date_text = _normalize_trade_date(trade_date)
    close_value = _numeric_or_none(close)
    if not db_code_text or not date_text or close_value is None:
        return

    matched_candidates = list(alias_to_candidates.get(db_code_text, []))
    if matched_candidates:
        for candidate in matched_candidates:
            _upsert_price_row(
                selected,
                stock_code=_safe_text(candidate.get("stock_code")),
                stock_name=_safe_text(candidate.get("stock_name")),
                trade_date=date_text,
                close=close_value,
                db_code=db_code_text,
                data_source=_safe_text(data_source),
                generated_at=timestamp,
            )
    elif include_database_scope:
        _upsert_price_row(
            selected,
            stock_code=db_code_text,
            stock_name="",
            trade_date=date_text,
            close=close_value,
            db_code=db_code_text,
            data_source=_safe_text(data_source),
            generated_at=timestamp,
        )


def _upsert_price_row(
    selected: Dict[tuple[str, str], Dict[str, Any]],
    *,
    stock_code: str,
    stock_name: str,
    trade_date: str,
    close: float,
    db_code: str,
    data_source: str,
    generated_at: str,
) -> None:
    if not stock_code:
        return
    key = (stock_code, trade_date)
    row = {
        "price_history_schema_version": PRICE_HISTORY_SCHEMA_VERSION,
        "generated_at": generated_at,
        "stock_code": stock_code,
        "stock_name": stock_name,
        "trade_date": trade_date,
        "close": close,
        "db_code": db_code,
        "data_source": data_source,
    }
    existing = selected.get(key)
    if existing is None or _alias_priority(stock_code, db_code) < _alias_priority(
        stock_code, _safe_text(existing.get("db_code"))
    ):
        selected[key] = _drop_empty(row)


def _audit_required_columns(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    missing_by_row = []
    for idx, row in enumerate(rows, start=1):
        missing = sorted(column for column in REQUIRED_PRICE_COLUMNS if row.get(column) in (None, ""))
        if missing:
            missing_by_row.append({"row": idx, "missing": missing})
    _add_check(
        checks,
        "price_history_required_columns",
        "PASS" if not missing_by_row else "FAIL",
        "all price-history rows include required columns"
        if not missing_by_row
        else f"{len(missing_by_row)} rows missing required columns",
        {"missing_by_row": missing_by_row[:20]},
    )


def _audit_candidate_coverage(
    checks: List[Dict[str, Any]],
    candidate_codes: set[str],
    row_codes: set[str],
) -> None:
    missing = sorted(candidate_codes - row_codes)
    _add_check(
        checks,
        "price_history_candidate_coverage",
        "PASS" if not missing else "FAIL",
        "all candidates have at least one close row"
        if not missing
        else f"{len(missing)} candidates missing price history",
        {"missing_stock_codes": missing[:50]},
    )


def _audit_anchor_coverage(
    checks: List[Dict[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    by_code_date: set[tuple[str, str]],
) -> None:
    missing = []
    for candidate in candidates:
        stock_code = _safe_text(candidate.get("stock_code"))
        for anchor in candidate.get("anchor_dates") or []:
            trade_date = _safe_text(anchor.get("trade_date")) if isinstance(anchor, Mapping) else ""
            if trade_date and (stock_code, trade_date) not in by_code_date:
                missing.append({"stock_code": stock_code, "trade_date": trade_date})
    _add_check(
        checks,
        "price_history_anchor_coverage",
        "PASS" if not missing else "WARN",
        "all candidate anchor dates have exact close rows"
        if not missing
        else f"{len(missing)} candidate anchor dates missing exact close rows",
        {"missing_anchors": missing[:50]},
    )


def _audit_forward_window_coverage(
    checks: List[Dict[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    dates_by_code: Dict[str, List[str]] = {}
    for row in rows:
        stock_code = _safe_text(row.get("stock_code"))
        trade_date = _safe_text(row.get("trade_date"))
        if stock_code and trade_date:
            dates_by_code.setdefault(stock_code, []).append(trade_date)
    dates_by_code = {code: sorted(set(dates)) for code, dates in dates_by_code.items()}

    incomplete = []
    for candidate in candidates:
        stock_code = _safe_text(candidate.get("stock_code"))
        dates = dates_by_code.get(stock_code, [])
        for anchor in candidate.get("anchor_dates") or []:
            if not isinstance(anchor, Mapping):
                continue
            trade_date = _safe_text(anchor.get("trade_date"))
            forward_days = _int_or_none(anchor.get("max_forward_trading_days")) or 0
            if not trade_date or forward_days <= 0 or trade_date not in dates:
                continue
            anchor_idx = dates.index(trade_date)
            available_forward_days = len(dates) - anchor_idx - 1
            if available_forward_days < forward_days:
                incomplete.append(
                    {
                        "stock_code": stock_code,
                        "trade_date": trade_date,
                        "required_forward_trading_days": forward_days,
                        "available_forward_trading_days": available_forward_days,
                    }
                )
    _add_check(
        checks,
        "price_history_forward_window_coverage",
        "PASS" if not incomplete else "WARN",
        "all queued anchors have enough forward closes for their max horizon"
        if not incomplete
        else f"{len(incomplete)} queued anchors still lack full forward windows",
        {"incomplete_windows": incomplete[:50]},
    )


def _audit_low_sensitivity(checks: List[Dict[str, Any]], rows: Sequence[Mapping[str, Any]]) -> None:
    findings = []
    for idx, row in enumerate(rows, start=1):
        for key, value in _walk(row):
            key_l = key.lower()
            text = _safe_text(value)
            if key_l in FORBIDDEN_RAW_KEYS or any(pattern.search(text) for pattern in SECRET_VALUE_PATTERNS):
                findings.append({"row": idx, "key": key})
    _add_check(
        checks,
        "price_history_low_sensitivity_boundary",
        "PASS" if not findings else "FAIL",
        "price-history rows contain only low-sensitivity close data"
        if not findings
        else f"{len(findings)} potentially sensitive fields or values found",
        {"findings": findings[:20]},
    )


def _sqlite_table_exists(con: sqlite3.Connection, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        [table_name],
    ).fetchone()
    return row is not None


def _normalize_scope(value: str) -> str:
    scope = _safe_text(value).lower() or "candidates"
    if scope not in {"candidates", "database"}:
        raise ValueError(f"unsupported price history scope: {value!r}")
    return scope


def _stock_code_aliases(stock_code: str) -> List[str]:
    text = stock_code.strip()
    if not text:
        return []
    upper = text.upper()
    aliases = {text, upper}
    normalized = upper.replace("_", ".")

    if re.fullmatch(r"\d{6}", normalized):
        aliases.update({normalized, f"{normalized}.SH", f"{normalized}.SZ", f"SH{normalized}", f"SZ{normalized}"})
    suffix_match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", normalized)
    if suffix_match:
        bare, exchange = suffix_match.groups()
        aliases.update({bare, f"{bare}.{exchange}", f"{exchange}{bare}"})
    prefix_match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", normalized)
    if prefix_match:
        exchange, bare = prefix_match.groups()
        aliases.update({bare, f"{bare}.{exchange}", f"{exchange}{bare}"})

    hk_match = re.fullmatch(r"(?:HK)?(\d{4,5})(?:\.HK)?", normalized)
    if hk_match:
        bare = hk_match.group(1).zfill(5)
        aliases.update({bare, bare[-4:], f"HK{bare}", f"{bare}.HK", f"{bare[-4:]}.HK"})

    return sorted(aliases)


def _alias_priority(stock_code: str, db_code: str) -> int:
    if db_code == stock_code:
        return 0
    if db_code.upper() == stock_code.upper():
        return 1
    return 2


def _price_history_file_date(
    rows: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    generated_at: Optional[datetime],
) -> str:
    trade_dates = [_safe_text(row.get("trade_date")) for row in rows if _safe_text(row.get("trade_date"))]
    if not trade_dates:
        for candidate in candidates:
            for anchor in candidate.get("anchor_dates") or []:
                if isinstance(anchor, Mapping) and _safe_text(anchor.get("trade_date")):
                    trade_dates.append(_safe_text(anchor.get("trade_date")))
    if trade_dates:
        return max(trade_dates).replace("-", "")
    return _iso_timestamp(generated_at)[:10].replace("-", "")


def _audit_file_date(audit: Mapping[str, Any]) -> str:
    trade_date = _safe_text(audit.get("latest_trade_date"))
    if trade_date:
        return trade_date.replace("-", "")
    return _safe_text(audit.get("generated_at"))[:10].replace("-", "") or "unknown"


def _load_optional_jsonl(path: Path | str | None) -> List[Dict[str, Any]]:
    if path is None:
        return []
    candidate = Path(path)
    if not candidate.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with candidate.open("r", encoding="utf-8") as file:
        for line_no, line in enumerate(file, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{candidate}:{line_no} is not valid JSONL") from exc
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            file.write("\n")


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = _fieldnames(rows, preferred=sorted(REQUIRED_PRICE_COLUMNS))
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})


def _write_checks_csv(path: Path, checks: Any) -> None:
    rows = checks if isinstance(checks, list) else []
    fieldnames = ["check", "status", "message", "details"]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for check in rows:
            check_map = check if isinstance(check, Mapping) else {}
            writer.writerow(
                {
                    "check": _safe_text(check_map.get("check")),
                    "status": _safe_text(check_map.get("status")),
                    "message": _safe_text(check_map.get("message")),
                    "details": json.dumps(check_map.get("details") or {}, ensure_ascii=False, sort_keys=True),
                }
            )


def _fieldnames(rows: Sequence[Mapping[str, Any]], *, preferred: Sequence[str]) -> List[str]:
    names: List[str] = []
    for name in preferred:
        if name not in names:
            names.append(name)
    for row in rows:
        for key in row:
            if key not in names:
                names.append(key)
    return names


def _add_check(
    checks: List[Dict[str, Any]],
    check: str,
    status: str,
    message: str,
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    checks.append(
        {
            "check": check,
            "status": status,
            "message": message,
            "details": dict(details or {}),
        }
    )


def _walk(value: Any, prefix: str = ""):
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            yield from _walk(item, f"{prefix}.{key_text}" if prefix else key_text)
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            yield from _walk(item, f"{prefix}[{idx}]")
    else:
        yield prefix, value


def _chunks(values: Sequence[str], size: int) -> List[List[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _normalize_trade_date(value: Any) -> str:
    text = _safe_text(value)
    if not text:
        return ""
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if len(text) >= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]):
        return text[:10]
    return text


def _numeric_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _int_or_none(value: Any) -> Optional[int]:
    numeric = _numeric_or_none(value)
    return int(numeric) if numeric is not None else None


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _drop_empty(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def _iso_timestamp(value: Optional[datetime]) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-path", default=str(DEFAULT_SNAPSHOT_PATH))
    parser.add_argument("--queue-path", default=str(DEFAULT_QUEUE_PATH))
    parser.add_argument("--database-path", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--scope",
        choices=("candidates", "database"),
        default="candidates",
        help="Export only snapshot/replay candidates or every stock_daily row in the database.",
    )
    parser.add_argument("--no-fail", action="store_true", help="Do not exit non-zero when the audit fails.")
    args = parser.parse_args(argv)

    candidates = load_candidate_specs(snapshot_path=args.snapshot_path, queue_path=args.queue_path)
    rows = build_price_history_rows(candidates, database_path=args.database_path, scope=args.scope)
    output_paths: List[Path] = []
    if rows:
        output_paths = write_price_history_outputs(
            snapshot_path=args.snapshot_path,
            queue_path=args.queue_path,
            database_path=args.database_path,
            output_dir=args.output_dir,
            scope=args.scope,
        )
    audit = audit_price_history(candidates, rows, database_path=args.database_path, scope=args.scope)
    audit_paths = write_price_history_audit_outputs(audit, args.output_dir)
    print(
        "ai_snapshot_price_history "
        f"rows={len(rows)} candidates={len(candidates)} "
        f"audit_status={audit['overall_status']} "
        f"outputs={','.join(str(path) for path in output_paths + audit_paths)}"
    )
    if audit["overall_status"] == "FAIL" and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
