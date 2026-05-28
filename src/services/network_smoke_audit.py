# -*- coding: utf-8 -*-
"""Structured PASS/WARN/FAIL audit for non-blocking network smoke logs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


NETWORK_SMOKE_AUDIT_SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = Path("reports") / "network_smoke"
DEFAULT_SOURCES = (
    ("pytest_network", Path("pytest-network.log"), Path("pytest-network.status")),
    ("quick_smoke", Path("quick-smoke.log"), Path("quick-smoke.status")),
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}"),
    re.compile(r"tvly-[A-Za-z0-9_-]{12,}"),
)
PYTEST_SUMMARY_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed|warnings?)",
    re.IGNORECASE,
)
QUOTA_PATTERNS = (
    re.compile(r"quota", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"exceeds your plan", re.IGNORECASE),
    re.compile(r"upgrade your plan", re.IGNORECASE),
)
NETWORK_ERROR_PATTERNS = (
    re.compile(r"DNS", re.IGNORECASE),
    re.compile(r"TLS", re.IGNORECASE),
    re.compile(r"SSL", re.IGNORECASE),
    re.compile(r"connection refused", re.IGNORECASE),
    re.compile(r"connection reset", re.IGNORECASE),
    re.compile(r"network", re.IGNORECASE),
    re.compile(r"timeout", re.IGNORECASE),
)
ERROR_PATTERNS = (
    re.compile(r"\bFAILED\b"),
    re.compile(r"\bERROR\b"),
    re.compile(r"Traceback \(most recent call last\)"),
)
PROVIDER_ENV_GROUPS = (
    {
        "provider": "deepseek",
        "category": "llm",
        "mode": "any",
        "vars": ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS", "LLM_DEEPSEEK_API_KEY", "LLM_DEEPSEEK_API_KEYS"),
    },
    {"provider": "tushare", "category": "market_data", "mode": "any", "vars": ("TUSHARE_TOKEN",)},
    {
        "provider": "longbridge",
        "category": "market_data",
        "mode": "all",
        "vars": ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"),
    },
    {"provider": "tavily", "category": "search", "mode": "any", "vars": ("TAVILY_API_KEYS",)},
    {"provider": "bocha", "category": "search", "mode": "any", "vars": ("BOCHA_API_KEYS",)},
    {"provider": "brave", "category": "search", "mode": "any", "vars": ("BRAVE_API_KEYS",)},
    {"provider": "serpapi", "category": "search", "mode": "any", "vars": ("SERPAPI_API_KEYS",)},
    {"provider": "anspire", "category": "search", "mode": "any", "vars": ("ANSPIRE_API_KEYS",)},
    {"provider": "minimax", "category": "search", "mode": "any", "vars": ("MINIMAX_API_KEYS",)},
    {"provider": "searxng", "category": "search", "mode": "any", "vars": ("SEARXNG_BASE_URLS",)},
)


def audit_network_smoke(
    *,
    sources: Sequence[tuple[str, Path | str, Path | str]] = DEFAULT_SOURCES,
    created_at: Optional[datetime] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Audit network smoke logs and sidecar exit-code files."""
    source_rows = [
        _inspect_source(name, Path(log_path), Path(status_path))
        for name, log_path, status_path in sources
    ]
    provider_env_rows = _inspect_provider_env(os.environ if environ is None else environ)
    checks: List[Dict[str, Any]] = []
    _audit_sources_present(checks, source_rows)
    _audit_exit_codes(checks, source_rows)
    _audit_pytest_summary(checks, source_rows)
    _audit_quota_signals(checks, source_rows)
    _audit_network_error_signals(checks, source_rows)
    _audit_secret_leakage(checks, source_rows)
    _audit_llm_provider_env(checks, provider_env_rows)
    _audit_market_data_provider_env(checks, provider_env_rows)
    _audit_search_provider_env(checks, provider_env_rows)
    _audit_partial_provider_env(checks, provider_env_rows)

    status_counts = dict(Counter(check["status"] for check in checks))
    overall_status = "FAIL" if status_counts.get("FAIL") else ("WARN" if status_counts.get("WARN") else "PASS")
    summary = _summarize_sources(source_rows)
    summary["provider_env"] = _summarize_provider_env(provider_env_rows)
    return {
        "audit_schema_version": NETWORK_SMOKE_AUDIT_SCHEMA_VERSION,
        "generated_at": _iso_timestamp(created_at),
        "overall_status": overall_status,
        "status_counts": status_counts,
        "summary": summary,
        "sources": source_rows,
        "provider_env": provider_env_rows,
        "checks": checks,
    }


def write_network_smoke_audit_outputs(
    audit: Mapping[str, Any],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> List[Path]:
    """Write latest and dated JSON/CSV/Markdown network-smoke audit artifacts."""
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    audit_date = _audit_file_date(audit)

    latest_json = target_dir / "network_smoke_audit_latest.json"
    dated_json = target_dir / f"network_smoke_audit_{audit_date}.json"
    latest_csv = target_dir / "network_smoke_audit_latest.csv"
    dated_csv = target_dir / f"network_smoke_audit_{audit_date}.csv"
    latest_md = target_dir / "network_smoke_audit_latest.md"
    dated_md = target_dir / f"network_smoke_audit_{audit_date}.md"

    payload = json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True)
    latest_json.write_text(payload + "\n", encoding="utf-8")
    if dated_json != latest_json:
        dated_json.write_text(payload + "\n", encoding="utf-8")

    _write_checks_csv(latest_csv, audit.get("checks", []))
    if dated_csv != latest_csv:
        _write_checks_csv(dated_csv, audit.get("checks", []))

    markdown = _render_markdown(audit)
    latest_md.write_text(markdown, encoding="utf-8")
    if dated_md != latest_md:
        dated_md.write_text(markdown, encoding="utf-8")

    return [latest_json, dated_json, latest_csv, dated_csv, latest_md, dated_md]


def _inspect_source(name: str, log_path: Path, status_path: Path) -> Dict[str, Any]:
    log_exists = log_path.exists()
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_exists else ""
    lines = text.splitlines()
    pytest_summary = _parse_pytest_summary(text)
    exit_code = _read_exit_code(status_path)
    return {
        "name": name,
        "log_path": str(log_path),
        "status_path": str(status_path),
        "log_exists": log_exists,
        "status_exists": status_path.exists(),
        "exit_code": exit_code,
        "line_count": len(lines),
        "pytest_summary": pytest_summary,
        "error_signal_count": _count_regex(lines, ERROR_PATTERNS),
        "quota_signal_count": _count_regex(lines, QUOTA_PATTERNS),
        "network_error_signal_count": _count_regex(lines, NETWORK_ERROR_PATTERNS),
        "secret_like_value_count": _count_secret_like_values(lines),
    }


def _inspect_provider_env(environ: Mapping[str, str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in PROVIDER_ENV_GROUPS:
        env_vars = tuple(str(item) for item in group["vars"])
        present_vars = [var for var in env_vars if _has_env_value(environ.get(var))]
        missing_vars = [var for var in env_vars if var not in present_vars]
        mode = str(group["mode"])
        if mode == "all":
            if len(present_vars) == len(env_vars):
                status = "configured"
            elif present_vars:
                status = "partial"
            else:
                status = "missing"
        else:
            status = "configured" if present_vars else "missing"
        rows.append(
            {
                "provider": group["provider"],
                "category": group["category"],
                "status": status,
                "mode": mode,
                "present_vars": present_vars,
                "missing_vars": missing_vars,
                "value_count": sum(_env_value_count(environ.get(var, "")) for var in present_vars),
            }
        )
    return rows


def _read_exit_code(path: Path) -> Optional[int]:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "exit_code":
            return _int_or_none(value.strip())
    return None


def _parse_pytest_summary(text: str) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for match in PYTEST_SUMMARY_RE.finditer(text):
        kind = match.group("kind").lower()
        if kind == "error":
            kind = "errors"
        elif kind == "warning":
            kind = "warnings"
        summary[kind] = summary.get(kind, 0) + int(match.group("count"))
    return summary


def _summarize_sources(sources: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    failed_sources = [
        source.get("name")
        for source in sources
        if source.get("exit_code") not in (0, None)
    ]
    missing_status = [source.get("name") for source in sources if not source.get("status_exists")]
    missing_logs = [source.get("name") for source in sources if not source.get("log_exists")]
    return {
        "source_count": len(sources),
        "failed_sources": failed_sources,
        "missing_status_sources": missing_status,
        "missing_log_sources": missing_logs,
        "error_signal_count": sum(_int_or_zero(source.get("error_signal_count")) for source in sources),
        "quota_signal_count": sum(_int_or_zero(source.get("quota_signal_count")) for source in sources),
        "network_error_signal_count": sum(
            _int_or_zero(source.get("network_error_signal_count")) for source in sources
        ),
        "secret_like_value_count": sum(_int_or_zero(source.get("secret_like_value_count")) for source in sources),
    }


def _summarize_provider_env(provider_env: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    configured = [str(row.get("provider")) for row in provider_env if row.get("status") == "configured"]
    partial = [str(row.get("provider")) for row in provider_env if row.get("status") == "partial"]
    missing = [str(row.get("provider")) for row in provider_env if row.get("status") == "missing"]
    return {
        "configured_count": len(configured),
        "partial_count": len(partial),
        "missing_count": len(missing),
        "configured_providers": configured,
        "partial_providers": partial,
        "missing_providers": missing,
    }


def _audit_sources_present(checks: List[Dict[str, Any]], sources: Sequence[Mapping[str, Any]]) -> None:
    missing_logs = [source.get("name") for source in sources if not source.get("log_exists")]
    missing_status = [source.get("name") for source in sources if not source.get("status_exists")]
    status = "FAIL" if missing_logs else ("WARN" if missing_status else "PASS")
    _add_check(
        checks,
        "network_smoke_artifacts_present",
        status,
        "network smoke logs/status files are present" if status == "PASS" else "network smoke artifacts are missing",
        {"missing_logs": missing_logs, "missing_status": missing_status},
    )


def _audit_exit_codes(checks: List[Dict[str, Any]], sources: Sequence[Mapping[str, Any]]) -> None:
    failed = [
        {"name": source.get("name"), "exit_code": source.get("exit_code")}
        for source in sources
        if source.get("exit_code") not in (0, None)
    ]
    unknown = [source.get("name") for source in sources if source.get("exit_code") is None]
    status = "FAIL" if failed else ("WARN" if unknown else "PASS")
    _add_check(
        checks,
        "network_smoke_exit_codes",
        status,
        "network smoke commands exited cleanly" if status == "PASS" else "network smoke exit codes need attention",
        {"failed": failed, "unknown": unknown},
    )


def _audit_pytest_summary(checks: List[Dict[str, Any]], sources: Sequence[Mapping[str, Any]]) -> None:
    failures = []
    for source in sources:
        summary = source.get("pytest_summary") if isinstance(source.get("pytest_summary"), Mapping) else {}
        failed = _int_or_zero(summary.get("failed")) + _int_or_zero(summary.get("errors"))
        if failed:
            failures.append({"name": source.get("name"), "failed_or_errors": failed})
    _add_check(
        checks,
        "pytest_network_summary",
        "FAIL" if failures else "PASS",
        "pytest summary has no failures" if not failures else "pytest summary contains failures or errors",
        {"failures": failures},
    )


def _audit_quota_signals(checks: List[Dict[str, Any]], sources: Sequence[Mapping[str, Any]]) -> None:
    count = sum(_int_or_zero(source.get("quota_signal_count")) for source in sources)
    _add_check(
        checks,
        "quota_rate_limit_signals",
        "WARN" if count else "PASS",
        f"quota/rate-limit signal count={count}",
        {"quota_signal_count": count},
    )


def _audit_network_error_signals(checks: List[Dict[str, Any]], sources: Sequence[Mapping[str, Any]]) -> None:
    count = sum(_int_or_zero(source.get("network_error_signal_count")) for source in sources)
    _add_check(
        checks,
        "network_error_signals",
        "WARN" if count else "PASS",
        f"network error signal count={count}",
        {"network_error_signal_count": count},
    )


def _audit_secret_leakage(checks: List[Dict[str, Any]], sources: Sequence[Mapping[str, Any]]) -> None:
    count = sum(_int_or_zero(source.get("secret_like_value_count")) for source in sources)
    _add_check(
        checks,
        "secret_like_values",
        "FAIL" if count else "PASS",
        f"secret-like value signal count={count}",
        {"secret_like_value_count": count},
    )


def _audit_llm_provider_env(checks: List[Dict[str, Any]], provider_env: Sequence[Mapping[str, Any]]) -> None:
    deepseek = _provider_env_by_name(provider_env, "deepseek")
    configured = deepseek.get("status") == "configured"
    _add_check(
        checks,
        "provider_env_deepseek",
        "PASS" if configured else "WARN",
        "DeepSeek provider env is configured" if configured else "DeepSeek provider env is missing",
        {
            "present_vars": deepseek.get("present_vars", []),
            "missing_vars": deepseek.get("missing_vars", []),
            "value_count": deepseek.get("value_count", 0),
        },
    )


def _audit_market_data_provider_env(checks: List[Dict[str, Any]], provider_env: Sequence[Mapping[str, Any]]) -> None:
    configured = _configured_providers(provider_env, "market_data")
    _add_check(
        checks,
        "provider_env_market_data",
        "PASS" if configured else "WARN",
        (
            f"credentialed market-data provider env configured: {', '.join(configured)}"
            if configured
            else "no credentialed market-data provider env configured; hosted run will rely on public fallbacks"
        ),
        {"configured_providers": configured},
    )


def _audit_search_provider_env(checks: List[Dict[str, Any]], provider_env: Sequence[Mapping[str, Any]]) -> None:
    configured = _configured_providers(provider_env, "search")
    _add_check(
        checks,
        "provider_env_search",
        "PASS" if configured else "WARN",
        (
            f"search provider env configured: {', '.join(configured)}"
            if configured
            else "no search provider env configured; hosted news search may be unavailable"
        ),
        {"configured_providers": configured},
    )


def _audit_partial_provider_env(checks: List[Dict[str, Any]], provider_env: Sequence[Mapping[str, Any]]) -> None:
    partial = [row for row in provider_env if row.get("status") == "partial"]
    _add_check(
        checks,
        "provider_env_partial_credentials",
        "WARN" if partial else "PASS",
        "provider env has partially configured credential groups" if partial else "no partial provider credential groups",
        {
            "partial_providers": [
                {
                    "provider": row.get("provider"),
                    "present_vars": row.get("present_vars", []),
                    "missing_vars": row.get("missing_vars", []),
                }
                for row in partial
            ]
        },
    )


def _add_check(
    checks: List[Dict[str, Any]],
    check: str,
    status: str,
    message: str,
    evidence: Mapping[str, Any],
) -> None:
    checks.append(
        {
            "check": check,
            "status": status,
            "message": message,
            "evidence": dict(evidence),
        }
    )


def _count_regex(lines: Sequence[str], patterns: Sequence[re.Pattern[str]]) -> int:
    return sum(1 for line in lines if any(pattern.search(line) for pattern in patterns))


def _count_secret_like_values(lines: Sequence[str]) -> int:
    count = 0
    for line in lines:
        if any(
            not _is_false_positive_secret_match(line, match)
            for pattern in SECRET_VALUE_PATTERNS
            for match in pattern.finditer(line)
        ):
            count += 1
    return count


def _is_false_positive_secret_match(line: str, match: re.Match[str]) -> bool:
    token = match.group(0).lower()
    if not token.startswith("sk-") or match.start() <= 0:
        return False
    return line[match.start() - 1] == "/"


def _provider_env_by_name(provider_env: Sequence[Mapping[str, Any]], provider: str) -> Mapping[str, Any]:
    for row in provider_env:
        if row.get("provider") == provider:
            return row
    return {}


def _configured_providers(provider_env: Sequence[Mapping[str, Any]], category: str) -> List[str]:
    return [
        str(row.get("provider"))
        for row in provider_env
        if row.get("category") == category and row.get("status") == "configured"
    ]


def _has_env_value(value: Any) -> bool:
    return bool(str(value or "").strip())


def _env_value_count(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    return len([part for part in text.split(",") if part.strip()])


def _write_checks_csv(path: Path, checks: Any) -> None:
    rows = checks if isinstance(checks, list) else []
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["check", "status", "message", "evidence_json"])
        writer.writeheader()
        for check in rows:
            check_map = check if isinstance(check, Mapping) else {}
            writer.writerow(
                {
                    "check": check_map.get("check", ""),
                    "status": check_map.get("status", ""),
                    "message": check_map.get("message", ""),
                    "evidence_json": json.dumps(check_map.get("evidence", {}), ensure_ascii=False, sort_keys=True),
                }
            )


def _render_markdown(audit: Mapping[str, Any]) -> str:
    summary = audit.get("summary") if isinstance(audit.get("summary"), Mapping) else {}
    provider_summary = summary.get("provider_env") if isinstance(summary.get("provider_env"), Mapping) else {}
    lines = [
        "# Network Smoke Audit",
        "",
        f"- Status: {audit.get('overall_status', '')}",
        f"- Generated at: {audit.get('generated_at', '')}",
        f"- Failed sources: {', '.join(summary.get('failed_sources', [])) if summary.get('failed_sources') else 'none'}",
        f"- Quota signals: {summary.get('quota_signal_count', 0)}",
        f"- Network error signals: {summary.get('network_error_signal_count', 0)}",
        (
            "- Provider env configured: "
            f"{', '.join(provider_summary.get('configured_providers', [])) if provider_summary.get('configured_providers') else 'none'}"
        ),
        (
            "- Provider env partial: "
            f"{', '.join(provider_summary.get('partial_providers', [])) if provider_summary.get('partial_providers') else 'none'}"
        ),
        "",
        "| Check | Status | Message |",
        "| --- | --- | --- |",
    ]
    checks = audit.get("checks") if isinstance(audit.get("checks"), list) else []
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        lines.append(
            f"| {_escape_markdown_table(check.get('check', ''))} "
            f"| {_escape_markdown_table(check.get('status', ''))} "
            f"| {_escape_markdown_table(check.get('message', ''))} |"
        )
    return "\n".join(lines) + "\n"


def _escape_markdown_table(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def _audit_file_date(audit: Mapping[str, Any]) -> str:
    generated_at = _safe_text(audit.get("generated_at"))
    if generated_at:
        return generated_at[:10].replace("-", "")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    parsed = _int_or_none(value)
    return parsed if parsed is not None else 0


def _iso_timestamp(value: Optional[datetime]) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit non-blocking network smoke logs.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--fail-on-fail", action="store_true", help="Return non-zero when FAIL checks exist.")
    args = parser.parse_args(argv)

    audit = audit_network_smoke()
    paths = write_network_smoke_audit_outputs(audit, args.output_dir)
    print(
        "network_smoke_audit "
        f"status={audit['overall_status']} "
        f"checks={audit['status_counts']} "
        f"outputs={', '.join(str(path) for path in paths)}"
    )
    return 1 if args.fail_on_fail and audit["overall_status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
