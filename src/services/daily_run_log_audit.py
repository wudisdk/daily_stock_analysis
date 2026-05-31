# -*- coding: utf-8 -*-
"""Structured PASS/WARN/FAIL audit for daily workflow logs."""

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


RUN_LOG_AUDIT_SCHEMA_VERSION = "1.0"
DEFAULT_LOG_DIR = Path("logs")
DEFAULT_OUTPUT_DIR = Path("reports") / "run_audit"
EXPECTED_MODEL = "deepseek/deepseek-v4-pro"
PROVIDER_ENV_GROUPS = (
    {
        "provider": "deepseek",
        "category": "llm",
        "mode": "any",
        "vars": ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS", "LLM_DEEPSEEK_API_KEY", "LLM_DEEPSEEK_API_KEYS"),
    },
    {
        "provider": "gemini_legacy",
        "category": "llm_legacy",
        "mode": "any",
        "vars": ("GEMINI_API_KEY", "GEMINI_API_KEYS", "GEMINI_MODEL", "GEMINI_MODEL_FALLBACK"),
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
SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"gh[opsu]_[A-Za-z0-9_]{20,}"),
)
QUOTA_PATTERNS = (
    re.compile(r"quota", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"set usage limit", re.IGNORECASE),
    re.compile(r"exceeds your plan", re.IGNORECASE),
    re.compile(r"upgrade your plan", re.IGNORECASE),
    re.compile(r"insufficient_quota", re.IGNORECASE),
    re.compile(r"billing", re.IGNORECASE),
)
TAVILY_COOLDOWN_PATTERNS = (
    re.compile(r"Tavily.*cooling down provider", re.IGNORECASE),
    re.compile(r"Tavily\s+暂停使用", re.IGNORECASE),
)
PROVIDER_KEY_BOOKKEEPING_PATTERNS = (
    re.compile(r"API Key .*错误计数", re.IGNORECASE),
    re.compile(r"API Key .*error count", re.IGNORECASE),
    re.compile(r"所有 API Key .*错误记录", re.IGNORECASE),
)
LEGACY_GEMINI_ENV_PATTERNS = (
    re.compile(r"\bGEMINI_API_KEYS?\s*:\s*(?:\*\*\*|[^\s-].*)", re.IGNORECASE),
    re.compile(r"\bGEMINI_MODEL(?:_FALLBACK)?\s*:\s*gemini", re.IGNORECASE),
    re.compile(r"Gemini API Key:\s*(?!.*(?:未配置|not configured|missing))", re.IGNORECASE),
)
LLM_ERROR_PATTERNS = (
    re.compile(r"LLM.*(?:失败|错误|error|failed)", re.IGNORECASE),
    re.compile(r"LiteLLM.*(?:error|failed)", re.IGNORECASE),
    re.compile(r"AuthenticationError", re.IGNORECASE),
    re.compile(r"APIConnectionError", re.IGNORECASE),
    re.compile(r"invalid api key", re.IGNORECASE),
    re.compile(r"all models.*failed", re.IGNORECASE),
    re.compile(r"所有.*模型.*失败", re.IGNORECASE),
)
FATAL_PATTERNS = (
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"\bCRITICAL\b", re.IGNORECASE),
    re.compile(r"Unhandled exception", re.IGNORECASE),
)
GENERIC_ERROR_RE = re.compile(r"\bERROR\b|错误")
WARNING_RE = re.compile(r"\bWARN(?:ING)?\b|警告", re.IGNORECASE)
MODEL_RE = re.compile(r"\b(?:deepseek|gemini|openai|anthropic|dashscope|zhipu|moonshot)/[A-Za-z0-9_.:/-]+")
LOG_DATE_RE = re.compile(r"stock_analysis_(\d{8})\.log$")
YFINANCE_REALTIME_SUCCESS_RE = re.compile(
    r"(?:Yfinance.*realtime quote.*succeeded|\[realtime_quote\].*success \(source: YfinanceFetcher\))",
    re.IGNORECASE,
)
YFINANCE_FALLBACK_LABEL_RE = re.compile(
    r"(?:实时行情|realtime quote|行情).*?(?:来源|source)[:：=]\s*fallback|source=fallback|realtime_provider_fallback",
    re.IGNORECASE,
)


def audit_run_logs(
    log_dir: Path | str = DEFAULT_LOG_DIR,
    *,
    max_files: int = 20,
    created_at: Optional[datetime] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Audit the latest daily stock-analysis logs without copying raw log lines."""
    directory = Path(log_dir)
    log_files = _find_log_files(directory, max_files=max_files)
    observations = [_inspect_log_file(path) for path in log_files]
    summary = _summarize_observations(observations)
    provider_env_rows = _inspect_provider_env(os.environ if environ is None else environ)
    summary["provider_env"] = _summarize_provider_env(provider_env_rows)

    checks: List[Dict[str, Any]] = []
    _audit_log_files(checks, directory, log_files)
    _audit_model_usage(checks, summary)
    _audit_legacy_gemini_env(checks, summary)
    _audit_provider_env_deepseek(checks, provider_env_rows)
    _audit_provider_env_gemini_legacy(checks, provider_env_rows)
    _audit_provider_env_market_data(checks, provider_env_rows)
    _audit_provider_env_search(checks, provider_env_rows)
    _audit_provider_env_partial(checks, provider_env_rows)
    _audit_llm_errors(checks, summary)
    _audit_search_quota(checks, summary)
    _audit_tavily_cooldown(checks, summary)
    _audit_provider_key_bookkeeping(checks, summary)
    _audit_realtime_fallback(checks, summary)
    _audit_yfinance_source_label(checks, summary)
    _audit_replay_artifact_visibility(checks, summary)
    _audit_runtime_errors(checks, summary)
    _audit_snapshot_audit_visibility(checks, summary)
    _audit_secret_leakage(checks, summary)

    status_counts = dict(Counter(check["status"] for check in checks))
    overall_status = "FAIL" if status_counts.get("FAIL") else ("WARN" if status_counts.get("WARN") else "PASS")
    generated_at = _iso_timestamp(created_at)
    log_dates = sorted({_safe_text(item.get("log_date")) for item in observations if item.get("log_date")})

    return {
        "audit_schema_version": RUN_LOG_AUDIT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "log_dir": str(directory),
        "overall_status": overall_status,
        "status_counts": status_counts,
        "log_file_count": len(log_files),
        "latest_log": str(log_files[0]) if log_files else None,
        "log_dates": log_dates,
        "latest_log_date": max(log_dates) if log_dates else None,
        "summary": summary,
        "provider_env": provider_env_rows,
        "checks": checks,
    }


def write_run_log_audit_outputs(
    audit: Mapping[str, Any],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
) -> List[Path]:
    """Write latest and dated JSON/CSV/Markdown audit artifacts."""
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    audit_date = _audit_file_date(audit)

    latest_json = target_dir / "daily_run_log_audit_latest.json"
    dated_json = target_dir / f"daily_run_log_audit_{audit_date}.json"
    latest_csv = target_dir / "daily_run_log_audit_latest.csv"
    dated_csv = target_dir / f"daily_run_log_audit_{audit_date}.csv"
    latest_md = target_dir / "daily_run_log_audit_latest.md"
    dated_md = target_dir / f"daily_run_log_audit_{audit_date}.md"

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


def _find_log_files(log_dir: Path, *, max_files: int) -> List[Path]:
    if not log_dir.exists():
        return []
    files = [path for path in log_dir.glob("stock_analysis_*.log") if path.is_file()]
    files.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    return files[:max(1, max_files)]


def _inspect_log_file(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    matched_models = sorted({match.group(0) for line in lines for match in MODEL_RE.finditer(line)})
    lower_models = [model.lower() for model in matched_models]

    return {
        "path": str(path),
        "name": path.name,
        "log_date": _log_date_from_name(path),
        "line_count": len(lines),
        "models": matched_models,
        "expected_model_count": _count_text(lines, EXPECTED_MODEL),
        "deepseek_model_count": sum(1 for model in lower_models if "deepseek/" in model),
        "gemini_model_count": sum(1 for model in lower_models if "gemini" in model),
        "flash_model_count": sum(1 for model in lower_models if "flash" in model),
        "legacy_gemini_env_count": _count_regex(lines, LEGACY_GEMINI_ENV_PATTERNS),
        "llm_success_count": _count_regex(lines, (re.compile(r"LLM.*(?:响应成功|success)", re.IGNORECASE),)),
        "llm_error_count": _count_regex(lines, LLM_ERROR_PATTERNS),
        "quota_issue_count": _count_regex(lines, QUOTA_PATTERNS),
        "tavily_quota_issue_count": _count_provider_quota(lines, "tavily"),
        "tavily_cooldown_count": _count_regex(lines, TAVILY_COOLDOWN_PATTERNS),
        "provider_key_bookkeeping_count": _count_regex(lines, PROVIDER_KEY_BOOKKEEPING_PATTERNS),
        "realtime_fallback_count": _count_regex(
            lines,
            (
                re.compile(r"realtime_provider_fallback", re.IGNORECASE),
                re.compile(r"source=fallback", re.IGNORECASE),
                re.compile(r"行情[:：]\s*fallback", re.IGNORECASE),
            ),
        ),
        "yfinance_realtime_success_count": _count_regex(lines, (YFINANCE_REALTIME_SUCCESS_RE,)),
        "yfinance_source_mismatch_count": _count_yfinance_source_mismatch(lines),
        "candidate_snapshot_export_count": _count_regex(
            lines,
            (re.compile(r"AI candidate snapshot exported|stock_ai_candidate_snapshot_latest", re.IGNORECASE),),
        ),
        "snapshot_audit_pass_count": _count_regex(
            lines,
            (re.compile(r"ai_snapshot_audit\s+status=PASS", re.IGNORECASE),),
        ),
        "snapshot_audit_warn_count": _count_regex(
            lines,
            (re.compile(r"ai_snapshot_audit\s+status=WARN", re.IGNORECASE),),
        ),
        "snapshot_audit_fail_count": _count_regex(
            lines,
            (re.compile(r"ai_snapshot_audit\s+status=FAIL", re.IGNORECASE),),
        ),
        "replay_queue_run_count": _count_regex(
            lines,
            (re.compile(r"ai_snapshot_replay_queue\b", re.IGNORECASE),),
        ),
        "replay_queue_fail_count": _count_regex(
            lines,
            (re.compile(r"ai_snapshot_replay_queue\b.*(?:audit_status=FAIL|rows=0)", re.IGNORECASE),),
        ),
        "price_history_run_count": _count_regex(
            lines,
            (re.compile(r"ai_snapshot_price_history\b", re.IGNORECASE),),
        ),
        "price_history_fail_count": _count_regex(
            lines,
            (re.compile(r"ai_snapshot_price_history\b.*(?:audit_status=FAIL|rows=0)", re.IGNORECASE),),
        ),
        "fatal_error_count": _count_regex(lines, FATAL_PATTERNS),
        "generic_error_count": _count_generic_runtime_errors(lines),
        "warning_count": _count_regex(lines, (WARNING_RE,)),
        "secret_like_value_count": _count_secret_like_values(lines),
    }


def _summarize_observations(observations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    totals: Dict[str, Any] = {
        "line_count": 0,
        "models": [],
        "expected_model_count": 0,
        "deepseek_model_count": 0,
        "gemini_model_count": 0,
        "flash_model_count": 0,
        "legacy_gemini_env_count": 0,
        "llm_success_count": 0,
        "llm_error_count": 0,
        "quota_issue_count": 0,
        "tavily_quota_issue_count": 0,
        "tavily_cooldown_count": 0,
        "provider_key_bookkeeping_count": 0,
        "realtime_fallback_count": 0,
        "yfinance_realtime_success_count": 0,
        "yfinance_source_mismatch_count": 0,
        "candidate_snapshot_export_count": 0,
        "snapshot_audit_pass_count": 0,
        "snapshot_audit_warn_count": 0,
        "snapshot_audit_fail_count": 0,
        "replay_queue_run_count": 0,
        "replay_queue_fail_count": 0,
        "price_history_run_count": 0,
        "price_history_fail_count": 0,
        "fatal_error_count": 0,
        "generic_error_count": 0,
        "warning_count": 0,
        "secret_like_value_count": 0,
    }
    model_set = set()
    numeric_fields = [key for key in totals if key not in {"models"}]
    for item in observations:
        for key in numeric_fields:
            totals[key] += _int_or_zero(item.get(key))
        model_set.update(str(model) for model in item.get("models", []) if model)
    totals["models"] = sorted(model_set)
    return totals


def _inspect_provider_env(environ: Mapping[str, str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in PROVIDER_ENV_GROUPS:
        vars_ = tuple(str(name) for name in group["vars"])
        configured = [name for name in vars_ if _env_has_value(environ.get(name))]
        if not configured:
            status = "missing"
        elif group.get("mode") == "all" and len(configured) < len(vars_):
            status = "partial"
        else:
            status = "configured"
        rows.append(
            {
                "provider": group["provider"],
                "category": group["category"],
                "status": status,
                "mode": group["mode"],
                "vars": list(vars_),
                "configured_vars": configured,
                "configured_count": len(configured),
                "required_count": len(vars_),
            }
        )
    return rows


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


def _audit_log_files(checks: List[Dict[str, Any]], log_dir: Path, log_files: Sequence[Path]) -> None:
    _add_check(
        checks,
        "log_files_found",
        "PASS" if log_files else "WARN",
        f"found {len(log_files)} stock analysis log file(s)",
        {"log_dir": str(log_dir), "latest_log": str(log_files[0]) if log_files else None},
    )


def _audit_model_usage(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    if _int_or_zero(summary.get("gemini_model_count")) or _int_or_zero(summary.get("flash_model_count")):
        status = "FAIL"
        message = "Gemini or flash model markers were found in daily logs"
    elif _int_or_zero(summary.get("expected_model_count")):
        status = "PASS"
        message = f"daily logs show expected model {EXPECTED_MODEL}"
    elif _int_or_zero(summary.get("deepseek_model_count")):
        status = "WARN"
        message = "daily logs show DeepSeek usage but not the expected pro model marker"
    else:
        status = "WARN"
        message = "daily logs do not show an LLM model marker"
    _add_check(
        checks,
        "llm_model_deepseek_pro",
        status,
        message,
        {
            "expected_model": EXPECTED_MODEL,
            "models": summary.get("models", []),
            "expected_model_count": summary.get("expected_model_count", 0),
            "gemini_model_count": summary.get("gemini_model_count", 0),
            "flash_model_count": summary.get("flash_model_count", 0),
        },
    )


def _audit_legacy_gemini_env(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    count = _int_or_zero(summary.get("legacy_gemini_env_count"))
    _add_check(
        checks,
        "legacy_gemini_env_blank",
        "WARN" if count else "PASS",
        "legacy GEMINI_* env markers are still configured in daily logs"
        if count
        else "legacy GEMINI_* env markers are blank or absent",
        {"legacy_gemini_env_count": count},
    )


def _audit_provider_env_deepseek(
    checks: List[Dict[str, Any]],
    provider_env: Sequence[Mapping[str, Any]],
) -> None:
    row = _provider_env_by_name(provider_env, "deepseek")
    configured = row.get("status") == "configured"
    _add_check(
        checks,
        "provider_env_deepseek",
        "PASS" if configured else "WARN",
        "DeepSeek API env is configured" if configured else "DeepSeek API env is missing",
        _provider_env_evidence(row),
    )


def _audit_provider_env_gemini_legacy(
    checks: List[Dict[str, Any]],
    provider_env: Sequence[Mapping[str, Any]],
) -> None:
    row = _provider_env_by_name(provider_env, "gemini_legacy")
    configured = row.get("status") == "configured"
    _add_check(
        checks,
        "provider_env_gemini_legacy_blank",
        "WARN" if configured else "PASS",
        "legacy Gemini env is still configured" if configured else "legacy Gemini env is blank",
        _provider_env_evidence(row),
    )


def _audit_provider_env_market_data(
    checks: List[Dict[str, Any]],
    provider_env: Sequence[Mapping[str, Any]],
) -> None:
    configured = _configured_providers(provider_env, "market_data")
    _add_check(
        checks,
        "provider_env_market_data",
        "PASS" if configured else "WARN",
        "at least one market-data provider env is configured"
        if configured
        else "no market-data provider env is configured",
        {"configured_providers": configured},
    )


def _audit_provider_env_search(
    checks: List[Dict[str, Any]],
    provider_env: Sequence[Mapping[str, Any]],
) -> None:
    configured = _configured_providers(provider_env, "search")
    _add_check(
        checks,
        "provider_env_search",
        "PASS" if configured else "WARN",
        "at least one search provider env is configured"
        if configured
        else "no search provider env is configured",
        {"configured_providers": configured},
    )


def _audit_provider_env_partial(
    checks: List[Dict[str, Any]],
    provider_env: Sequence[Mapping[str, Any]],
) -> None:
    partial = [row for row in provider_env if row.get("status") == "partial"]
    _add_check(
        checks,
        "provider_env_partial_credentials",
        "WARN" if partial else "PASS",
        "some provider credential groups are only partially configured"
        if partial
        else "no partial provider credential groups detected",
        {"partial_providers": [_provider_env_evidence(row) for row in partial]},
    )


def _audit_llm_errors(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    count = _int_or_zero(summary.get("llm_error_count"))
    _add_check(
        checks,
        "llm_errors",
        "WARN" if count else "PASS",
        f"LLM error signal count={count}",
        {"llm_error_count": count, "llm_success_count": summary.get("llm_success_count", 0)},
    )


def _audit_search_quota(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    count = _int_or_zero(summary.get("quota_issue_count"))
    tavily_count = _int_or_zero(summary.get("tavily_quota_issue_count"))
    _add_check(
        checks,
        "search_provider_quota",
        "WARN" if count else "PASS",
        f"search quota/rate-limit signal count={count}",
        {"quota_issue_count": count, "tavily_quota_issue_count": tavily_count},
    )


def _audit_tavily_cooldown(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    tavily_quota_count = _int_or_zero(summary.get("tavily_quota_issue_count"))
    cooldown_count = _int_or_zero(summary.get("tavily_cooldown_count"))
    if tavily_quota_count and not cooldown_count:
        status = "WARN"
        message = "Tavily quota signals found but cooldown marker was not visible"
    elif cooldown_count:
        status = "PASS"
        message = "Tavily cooldown marker was visible after quota/plan-limit signal"
    else:
        status = "PASS"
        message = "no Tavily quota cooldown signal needed"
    _add_check(
        checks,
        "tavily_quota_cooldown_visible",
        status,
        message,
        {"tavily_quota_issue_count": tavily_quota_count, "tavily_cooldown_count": cooldown_count},
    )


def _audit_provider_key_bookkeeping(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    count = _int_or_zero(summary.get("provider_key_bookkeeping_count"))
    _add_check(
        checks,
        "provider_key_bookkeeping",
        "WARN" if count else "PASS",
        f"provider API-key bookkeeping signal count={count}",
        {"provider_key_bookkeeping_count": count},
    )


def _audit_realtime_fallback(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    count = _int_or_zero(summary.get("realtime_fallback_count"))
    _add_check(
        checks,
        "realtime_quote_fallback",
        "WARN" if count else "PASS",
        f"realtime quote fallback signal count={count}",
        {"realtime_fallback_count": count},
    )


def _audit_yfinance_source_label(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    mismatch_count = _int_or_zero(summary.get("yfinance_source_mismatch_count"))
    success_count = _int_or_zero(summary.get("yfinance_realtime_success_count"))
    _add_check(
        checks,
        "yfinance_source_label",
        "WARN" if mismatch_count else "PASS",
        (
            "successful YFinance realtime quote was later labeled as fallback"
            if mismatch_count
            else "no YFinance realtime success/fallback label mismatch found"
        ),
        {
            "yfinance_realtime_success_count": success_count,
            "yfinance_source_mismatch_count": mismatch_count,
        },
    )


def _audit_replay_artifact_visibility(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    snapshot_exports = _int_or_zero(summary.get("candidate_snapshot_export_count"))
    snapshot_audit_passes = _int_or_zero(summary.get("snapshot_audit_pass_count"))
    snapshot_audit_warns = _int_or_zero(summary.get("snapshot_audit_warn_count"))
    snapshot_audit_fails = _int_or_zero(summary.get("snapshot_audit_fail_count"))
    queue_runs = _int_or_zero(summary.get("replay_queue_run_count"))
    queue_fails = _int_or_zero(summary.get("replay_queue_fail_count"))
    price_runs = _int_or_zero(summary.get("price_history_run_count"))
    price_fails = _int_or_zero(summary.get("price_history_fail_count"))
    snapshot_audit_runs = snapshot_audit_passes + snapshot_audit_warns + snapshot_audit_fails
    should_have_replay = bool(snapshot_exports or snapshot_audit_runs)
    if queue_fails or price_fails:
        status = "FAIL"
        message = "AI replay artifact step reported failure"
    elif should_have_replay and queue_runs and price_runs:
        status = "PASS"
        message = "AI replay queue and price-history markers are visible"
    elif should_have_replay:
        status = "WARN"
        message = "candidate snapshot was exported but replay artifact markers are missing"
    else:
        status = "PASS"
        message = "no candidate snapshot export marker found; replay artifacts not required"
    _add_check(
        checks,
        "ai_replay_artifacts_visible",
        status,
        message,
        {
            "candidate_snapshot_export_count": snapshot_exports,
            "snapshot_audit_pass_count": snapshot_audit_passes,
            "snapshot_audit_warn_count": snapshot_audit_warns,
            "snapshot_audit_fail_count": snapshot_audit_fails,
            "replay_queue_run_count": queue_runs,
            "replay_queue_fail_count": queue_fails,
            "price_history_run_count": price_runs,
            "price_history_fail_count": price_fails,
        },
    )


def _audit_runtime_errors(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    fatal_count = _int_or_zero(summary.get("fatal_error_count"))
    generic_count = _int_or_zero(summary.get("generic_error_count"))
    if fatal_count:
        status = "FAIL"
        message = f"fatal runtime error signal count={fatal_count}"
    elif generic_count:
        status = "WARN"
        message = f"generic ERROR signal count={generic_count}"
    else:
        status = "PASS"
        message = "no fatal or generic ERROR signals found"
    _add_check(
        checks,
        "runtime_errors",
        status,
        message,
        {"fatal_error_count": fatal_count, "generic_error_count": generic_count},
    )


def _audit_snapshot_audit_visibility(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    pass_count = _int_or_zero(summary.get("snapshot_audit_pass_count"))
    warn_count = _int_or_zero(summary.get("snapshot_audit_warn_count"))
    fail_count = _int_or_zero(summary.get("snapshot_audit_fail_count"))
    if fail_count:
        status = "FAIL"
        message = "snapshot audit FAIL marker found in logs"
    elif pass_count:
        status = "PASS"
        message = "snapshot audit PASS marker found in logs"
    elif warn_count:
        status = "PASS"
        message = "snapshot audit WARN marker found in logs"
    else:
        status = "WARN"
        message = "snapshot audit marker not found in logs"
    _add_check(
        checks,
        "snapshot_audit_visible",
        status,
        message,
        {
            "snapshot_audit_pass_count": pass_count,
            "snapshot_audit_warn_count": warn_count,
            "snapshot_audit_fail_count": fail_count,
        },
    )


def _audit_secret_leakage(checks: List[Dict[str, Any]], summary: Mapping[str, Any]) -> None:
    count = _int_or_zero(summary.get("secret_like_value_count"))
    _add_check(
        checks,
        "secret_like_values",
        "FAIL" if count else "PASS",
        f"secret-like value signal count={count}",
        {"secret_like_value_count": count},
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


def _count_text(lines: Sequence[str], needle: str) -> int:
    lowered = needle.lower()
    return sum(1 for line in lines if lowered in line.lower())


def _count_regex(lines: Sequence[str], patterns: Sequence[re.Pattern[str]]) -> int:
    return sum(1 for line in lines if any(pattern.search(line) for pattern in patterns))


def _line_matches_any(line: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(pattern.search(line) for pattern in patterns)


def _count_provider_quota(lines: Sequence[str], provider: str) -> int:
    provider_lower = provider.lower()
    return sum(
        1
        for line in lines
        if provider_lower in line.lower() and any(pattern.search(line) for pattern in QUOTA_PATTERNS)
    )


def _count_yfinance_source_mismatch(lines: Sequence[str]) -> int:
    mismatches = 0
    yfinance_success_window = 0
    for line in lines:
        if YFINANCE_REALTIME_SUCCESS_RE.search(line):
            yfinance_success_window = 6
            continue
        if yfinance_success_window <= 0:
            continue
        if YFINANCE_FALLBACK_LABEL_RE.search(line):
            mismatches += 1
            yfinance_success_window = 0
            continue
        yfinance_success_window -= 1
    return mismatches


def _count_generic_runtime_errors(lines: Sequence[str]) -> int:
    count = 0
    for line in lines:
        if not GENERIC_ERROR_RE.search(line):
            continue
        if _line_matches_any(line, QUOTA_PATTERNS):
            continue
        if _line_matches_any(line, PROVIDER_KEY_BOOKKEEPING_PATTERNS):
            continue
        count += 1
    return count


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


def _env_has_value(value: Optional[str]) -> bool:
    if value is None:
        return False
    stripped = str(value).strip()
    return bool(stripped and stripped not in {"-", "''", '""'})


def _provider_env_by_name(
    provider_env: Sequence[Mapping[str, Any]],
    provider: str,
) -> Mapping[str, Any]:
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


def _provider_env_evidence(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "provider": row.get("provider"),
        "category": row.get("category"),
        "status": row.get("status"),
        "mode": row.get("mode"),
        "vars": list(row.get("vars") or []),
        "configured_vars": list(row.get("configured_vars") or []),
        "configured_count": row.get("configured_count", 0),
        "required_count": row.get("required_count", 0),
    }


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
        "# Daily Run Log Audit",
        "",
        f"- Status: {audit.get('overall_status', '')}",
        f"- Generated at: {audit.get('generated_at', '')}",
        f"- Latest log: {audit.get('latest_log') or 'none'}",
        f"- Models: {', '.join(summary.get('models', [])) if summary.get('models') else 'none'}",
        f"- Configured providers: {', '.join(provider_summary.get('configured_providers', [])) if provider_summary.get('configured_providers') else 'none'}",
        f"- Partial provider credentials: {', '.join(provider_summary.get('partial_providers', [])) if provider_summary.get('partial_providers') else 'none'}",
        f"- Missing provider groups: {provider_summary.get('missing_count', 0)}",
        f"- Legacy Gemini env markers: {summary.get('legacy_gemini_env_count', 0)}",
        f"- Quota signals: {summary.get('quota_issue_count', 0)}",
        f"- Tavily cooldown signals: {summary.get('tavily_cooldown_count', 0)}",
        f"- Provider key bookkeeping signals: {summary.get('provider_key_bookkeeping_count', 0)}",
        f"- Realtime fallback signals: {summary.get('realtime_fallback_count', 0)}",
        f"- YFinance fallback-label mismatches: {summary.get('yfinance_source_mismatch_count', 0)}",
        f"- Replay queue markers: {summary.get('replay_queue_run_count', 0)}",
        f"- Price-history markers: {summary.get('price_history_run_count', 0)}",
        f"- Runtime error signals: {summary.get('generic_error_count', 0)}",
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
    latest_log_date = _safe_text(audit.get("latest_log_date"))
    if latest_log_date:
        return latest_log_date
    generated_at = _safe_text(audit.get("generated_at"))
    if generated_at:
        return generated_at[:10].replace("-", "")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _log_date_from_name(path: Path) -> Optional[str]:
    match = LOG_DATE_RE.search(path.name)
    return match.group(1) if match else None


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


def _iso_timestamp(value: Optional[datetime]) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit daily stock-analysis workflow logs.")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument("--fail-on-fail", action="store_true", help="Return non-zero when FAIL checks exist.")
    args = parser.parse_args(argv)

    audit = audit_run_logs(args.log_dir, max_files=args.max_files)
    paths = write_run_log_audit_outputs(audit, args.output_dir)
    print(
        "daily_run_log_audit "
        f"status={audit['overall_status']} "
        f"logs={audit['log_file_count']} "
        f"checks={audit['status_counts']} "
        f"outputs={', '.join(str(path) for path in paths)}"
    )
    return 1 if args.fail_on_fail and audit["overall_status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
