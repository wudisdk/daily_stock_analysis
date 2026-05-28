# -*- coding: utf-8 -*-
"""Tests for daily workflow log audit artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from src.services.daily_run_log_audit import audit_run_logs, write_run_log_audit_outputs


def _provider_env() -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": "deepseek-secret",
        "TUSHARE_TOKEN": "tushare-secret",
        "TAVILY_API_KEYS": "tavily-secret",
    }


def test_audit_run_logs_passes_clean_deepseek_pro_log(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stock_analysis_20260529.log").write_text(
        "\n".join(
            [
                "[LLM config] model: deepseek/deepseek-v4-pro",
                "[LLM response] deepseek/deepseek-v4-pro success",
                "ai_snapshot_audit status=PASS rows=3 checks={'PASS': 12}",
                "ai_snapshot_replay_queue rows=15 candidates=3 horizons=5 audit_status=PASS outputs=reports/ai_snapshot/x.json",
                "ai_snapshot_price_history rows=114 candidates=3 audit_status=PASS outputs=reports/ai_snapshot/y.json",
            ]
        ),
        encoding="utf-8",
    )

    audit = audit_run_logs(
        log_dir,
        created_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
        environ=_provider_env(),
    )

    assert audit["overall_status"] == "PASS"
    assert audit["latest_log_date"] == "20260529"
    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["llm_model_deepseek_pro"]["status"] == "PASS"
    assert checks["legacy_gemini_env_blank"]["status"] == "PASS"
    assert checks["provider_env_deepseek"]["status"] == "PASS"
    assert checks["provider_env_gemini_legacy_blank"]["status"] == "PASS"
    assert checks["provider_env_market_data"]["status"] == "PASS"
    assert checks["provider_env_search"]["status"] == "PASS"
    assert checks["provider_env_partial_credentials"]["status"] == "PASS"
    assert checks["tavily_quota_cooldown_visible"]["status"] == "PASS"
    assert checks["provider_key_bookkeeping"]["status"] == "PASS"
    assert checks["yfinance_source_label"]["status"] == "PASS"
    assert checks["ai_replay_artifacts_visible"]["status"] == "PASS"
    assert checks["snapshot_audit_visible"]["status"] == "PASS"


def test_audit_run_logs_warns_for_tavily_quota_and_realtime_fallback(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stock_analysis_20260529.log").write_text(
        "\n".join(
            [
                "[LLM config] model: deepseek/deepseek-v4-pro",
                "[Tavily] This request exceeds your plan's set usage limit. Please upgrade your plan",
                "[Tavily] API Key tvly-dev error count: 1",
                "[realtime_quote] HK HK00981 success (source: YfinanceFetcher)",
                "HK00981 realtime quote source=fallback",
                "realtime_provider_fallback source=fallback",
                "AI candidate snapshot exported: reports/ai_snapshot/stock_ai_candidate_snapshot_latest.jsonl",
            ]
        ),
        encoding="utf-8",
    )

    audit = audit_run_logs(log_dir, environ=_provider_env())

    assert audit["overall_status"] == "WARN"
    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["search_provider_quota"]["status"] == "WARN"
    assert checks["tavily_quota_cooldown_visible"]["status"] == "WARN"
    assert checks["provider_key_bookkeeping"]["status"] == "WARN"
    assert checks["realtime_quote_fallback"]["status"] == "WARN"
    assert checks["yfinance_source_label"]["status"] == "WARN"
    assert checks["ai_replay_artifacts_visible"]["status"] == "WARN"
    assert checks["runtime_errors"]["status"] == "PASS"
    assert audit["summary"]["tavily_quota_issue_count"] == 1
    assert audit["summary"]["provider_key_bookkeeping_count"] == 1
    assert audit["summary"]["yfinance_source_mismatch_count"] == 1


def test_audit_run_logs_reports_safe_provider_env_inventory(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stock_analysis_20260529.log").write_text(
        "\n".join(
            [
                "[LLM config] model: deepseek/deepseek-v4-pro",
                "ai_snapshot_audit status=PASS rows=3 checks={'PASS': 12}",
                "ai_snapshot_replay_queue rows=15 candidates=3 horizons=5 audit_status=PASS outputs=x",
                "ai_snapshot_price_history rows=114 candidates=3 audit_status=PASS outputs=y",
            ]
        ),
        encoding="utf-8",
    )
    environ = {
        "DEEPSEEK_API_KEYS": "secret-a,secret-b",
        "GEMINI_API_KEY": "legacy-secret",
        "TUSHARE_TOKEN": "tushare-secret",
        "LONGBRIDGE_APP_KEY": "lb-key",
        "TAVILY_API_KEYS": "tavily-secret",
    }

    audit = audit_run_logs(log_dir, environ=environ)

    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["provider_env_deepseek"]["status"] == "PASS"
    assert checks["provider_env_gemini_legacy_blank"]["status"] == "WARN"
    assert checks["provider_env_market_data"]["status"] == "PASS"
    assert checks["provider_env_search"]["status"] == "PASS"
    assert checks["provider_env_partial_credentials"]["status"] == "WARN"
    assert audit["summary"]["provider_env"]["configured_providers"] == [
        "deepseek",
        "gemini_legacy",
        "tushare",
        "tavily",
    ]
    assert audit["summary"]["provider_env"]["partial_providers"] == ["longbridge"]
    rendered = json.dumps(audit, ensure_ascii=False)
    assert "secret-a" not in rendered
    assert "legacy-secret" not in rendered
    assert "tushare-secret" not in rendered
    assert "tavily-secret" not in rendered


def test_audit_run_logs_warns_when_legacy_gemini_env_is_still_configured(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stock_analysis_20260529.log").write_text(
        "\n".join(
            [
                "[LLM config] model: deepseek/deepseek-v4-pro",
                "GEMINI_API_KEY: ***",
                "GEMINI_MODEL: gemini-3-flash-preview",
                "GEMINI_MODEL_FALLBACK: gemini-2.5-flash",
                "DeepSeek Key: configured",
            ]
        ),
        encoding="utf-8",
    )

    audit = audit_run_logs(log_dir, environ=_provider_env())

    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["llm_model_deepseek_pro"]["status"] == "PASS"
    assert checks["legacy_gemini_env_blank"]["status"] == "WARN"
    assert checks["legacy_gemini_env_blank"]["evidence"]["legacy_gemini_env_count"] == 3
    assert audit["summary"]["legacy_gemini_env_count"] == 3


def test_audit_run_logs_marks_tavily_cooldown_visible(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stock_analysis_20260529.log").write_text(
        "\n".join(
            [
                "[LLM config] model: deepseek/deepseek-v4-pro",
                "[Tavily] This request exceeds your plan's set usage limit. Please upgrade your plan",
                "Tavily hit quota signal; cooling down provider for 21600s",
                "ai_snapshot_audit status=PASS rows=3 checks={'PASS': 12}",
                "ai_snapshot_replay_queue rows=15 candidates=3 horizons=5 audit_status=PASS outputs=reports/ai_snapshot/x.json",
                "ai_snapshot_price_history rows=114 candidates=3 audit_status=PASS outputs=reports/ai_snapshot/y.json",
            ]
        ),
        encoding="utf-8",
    )

    audit = audit_run_logs(log_dir)

    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["search_provider_quota"]["status"] == "WARN"
    assert checks["tavily_quota_cooldown_visible"]["status"] == "PASS"
    assert audit["summary"]["tavily_cooldown_count"] == 1


def test_audit_run_logs_fails_for_flash_traceback_and_secret_like_value(tmp_path) -> None:
    secret_value = "sk-" + "redacted-placeholder-123456"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stock_analysis_20260529.log").write_text(
        "\n".join(
            [
                "[LLM config] model: gemini/gemini-2.5-flash",
                "Traceback (most recent call last)",
                f"Authorization: {secret_value}",
            ]
        ),
        encoding="utf-8",
    )

    audit = audit_run_logs(log_dir)

    assert audit["overall_status"] == "FAIL"
    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["llm_model_deepseek_pro"]["status"] == "FAIL"
    assert checks["runtime_errors"]["status"] == "FAIL"
    assert checks["secret_like_values"]["status"] == "FAIL"


def test_audit_outputs_do_not_copy_secret_like_log_values(tmp_path) -> None:
    secret_value = "sk-" + "redacted-placeholder-123456"
    log_dir = tmp_path / "logs"
    output_dir = tmp_path / "out"
    log_dir.mkdir()
    (log_dir / "stock_analysis_20260529.log").write_text(
        f"[LLM config] model: deepseek/deepseek-v4-pro\nAuthorization: {secret_value}\n",
        encoding="utf-8",
    )

    audit = audit_run_logs(log_dir)
    write_run_log_audit_outputs(audit, output_dir)

    for path in output_dir.iterdir():
        assert secret_value not in path.read_text(encoding="utf-8-sig")


def test_audit_secret_detection_ignores_news_urls_with_sk_slugs(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stock_analysis_20260529.log").write_text(
        "\n".join(
            [
                "[LLM config] model: deepseek/deepseek-v4-pro",
                "https://www.cnbc.com/2026/05/27/sk-hynix-hits-ai-chip-milestone.html",
                "https://www.reuters.com/world/asia-pacific/sk-hynix-joins-market-cap-club/",
            ]
        ),
        encoding="utf-8",
    )

    audit = audit_run_logs(log_dir)

    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["secret_like_values"]["status"] == "PASS"
    assert audit["summary"]["secret_like_value_count"] == 0


def test_write_run_log_audit_outputs_writes_latest_dated_json_csv_md(tmp_path) -> None:
    audit = {
        "generated_at": "2026-05-29T00:00:00+00:00",
        "latest_log_date": "20260529",
        "overall_status": "PASS",
        "status_counts": {"PASS": 1},
        "log_file_count": 1,
        "latest_log": "logs/stock_analysis_20260529.log",
        "summary": {"models": ["deepseek/deepseek-v4-pro"]},
        "checks": [
            {
                "check": "llm_model_deepseek_pro",
                "status": "PASS",
                "message": "ok",
                "evidence": {"expected_model": "deepseek/deepseek-v4-pro"},
            }
        ],
    }

    paths = write_run_log_audit_outputs(audit, tmp_path)
    names = {path.name for path in paths}

    assert names == {
        "daily_run_log_audit_latest.json",
        "daily_run_log_audit_20260529.json",
        "daily_run_log_audit_latest.csv",
        "daily_run_log_audit_20260529.csv",
        "daily_run_log_audit_latest.md",
        "daily_run_log_audit_20260529.md",
    }
    assert json.loads((tmp_path / "daily_run_log_audit_latest.json").read_text(encoding="utf-8"))[
        "overall_status"
    ] == "PASS"
    assert "llm_model_deepseek_pro" in (tmp_path / "daily_run_log_audit_latest.csv").read_text(
        encoding="utf-8-sig"
    )
    assert "Daily Run Log Audit" in (tmp_path / "daily_run_log_audit_latest.md").read_text(encoding="utf-8")
