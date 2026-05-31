# -*- coding: utf-8 -*-
"""Static checks for LLM provider channel mappings in 00-daily-analysis.yml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT_DIR / "apps/dsa-web/src/components/settings/llmProviderTemplates.ts"
WORKFLOW_PATH = ROOT_DIR / ".github/workflows/00-daily-analysis.yml"
NETWORK_SMOKE_PATH = ROOT_DIR / ".github/workflows/network-smoke.yml"
ENV_EXAMPLE_PATH = ROOT_DIR / ".env.example"

EXPECTED_TEMPLATE_CHANNELS = {
    "aihubmix",
    "deepseek",
    "dashscope",
    "zhipu",
    "moonshot",
    "minimax",
    "volcengine",
    "siliconflow",
    "openrouter",
    "gemini",
    "anthropic",
    "openai",
    "ollama",
}


def _extract_provider_templates() -> dict[str, str]:
    content = TEMPLATE_PATH.read_text(encoding="utf-8")
    matches = re.findall(
        r"channelId:\s*'(?P<channel>[^']+)'.*?baseUrl:\s*'(?P<base_url>[^']*)'",
        content,
        flags=re.DOTALL,
    )
    assert matches, "No provider channelId entries were found in llmProviderTemplates.ts"

    templates = {channel: base_url for channel, base_url in matches if channel != "custom"}
    assert EXPECTED_TEMPLATE_CHANNELS.issubset(templates.keys())
    assert "ark" not in templates
    return templates


def _load_daily_analysis_env() -> dict[str, str]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["analyze"]["steps"]
    analyze_step = next((step for step in steps if step.get("name") == "执行股票分析"), None)
    available_step_names = [step.get("name", "<unnamed>") for step in steps]
    assert analyze_step is not None, (
        "Expected 00-daily-analysis.yml job analyze to include a step named "
        f"'执行股票分析'; available step names: {available_step_names}"
    )
    return analyze_step["env"]


def _load_network_smoke_envs() -> list[dict[str, str]]:
    workflow = yaml.safe_load(NETWORK_SMOKE_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["smoke"]["steps"]
    env_steps = [
        step["env"]
        for step in steps
        if step.get("name") in {
            "Run pytest network smoke (non-blocking)",
            "Run quick smoke (non-blocking)",
        }
    ]
    assert len(env_steps) == 2
    return env_steps


def _load_network_smoke_steps() -> list[dict]:
    workflow = yaml.safe_load(NETWORK_SMOKE_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["smoke"]["steps"]
    smoke_steps = [
        step
        for step in steps
        if step.get("name") in {
            "Run pytest network smoke (non-blocking)",
            "Run quick smoke (non-blocking)",
        }
    ]
    assert len(smoke_steps) == 2
    return smoke_steps


def test_daily_analysis_maps_all_provider_template_channels() -> None:
    templates = _extract_provider_templates()
    env = _load_daily_analysis_env()

    for channel in templates:
        prefix = f"LLM_{channel.upper()}_"
        for suffix in (
            "PROTOCOL",
            "BASE_URL",
            "API_KEY",
            "API_KEYS",
            "MODELS",
            "ENABLED",
            "EXTRA_HEADERS",
        ):
            assert f"{prefix}{suffix}" in env

    assert not any(key.startswith("LLM_ARK_") for key in env)


def test_daily_analysis_keeps_channel_secrets_in_secrets_context() -> None:
    templates = _extract_provider_templates()
    env = _load_daily_analysis_env()

    for channel in templates:
        upper = channel.upper()
        for suffix in ("API_KEY", "API_KEYS"):
            key = f"LLM_{upper}_{suffix}"
            assert env[key] == f"${{{{ secrets.{key} }}}}"

        for suffix in ("PROTOCOL", "BASE_URL", "MODELS", "ENABLED", "EXTRA_HEADERS"):
            key = f"LLM_{upper}_{suffix}"
            assert f"vars.{key}" in env[key]
            assert f"secrets.{key}" in env[key]


def test_daily_analysis_defaults_to_deepseek_v4_and_stable_search() -> None:
    env = _load_daily_analysis_env()

    assert env["LITELLM_MODEL"] == (
        "${{ vars.LITELLM_MODEL || secrets.LITELLM_MODEL || 'deepseek/deepseek-v4-pro' }}"
    )
    assert env["LITELLM_FALLBACK_MODELS"] == (
        "${{ vars.LITELLM_FALLBACK_MODELS || secrets.LITELLM_FALLBACK_MODELS || 'deepseek/deepseek-v4-pro' }}"
    )
    assert env["LITELLM_TIMEOUT_SECONDS"] == (
        "${{ vars.LITELLM_TIMEOUT_SECONDS || secrets.LITELLM_TIMEOUT_SECONDS || '120' }}"
    )
    assert env["LITELLM_LOG_LEVEL"] == (
        "${{ vars.LITELLM_LOG_LEVEL || secrets.LITELLM_LOG_LEVEL || 'ERROR' }}"
    )
    assert env["SEARXNG_PUBLIC_INSTANCES_ENABLED"] == (
        "${{ vars.SEARXNG_PUBLIC_INSTANCES_ENABLED || secrets.SEARXNG_PUBLIC_INSTANCES_ENABLED || 'true' }}"
    )
    assert env["REALTIME_SOURCE_PRIORITY"] == (
        "${{ vars.REALTIME_SOURCE_PRIORITY || 'tushare,tencent,akshare_sina,efinance,akshare_em' }}"
    )
    assert env["GEMINI_API_KEY"] == ""
    assert env["GEMINI_API_KEYS"] == ""
    assert env["GEMINI_MODEL"] == ""
    assert env["GEMINI_MODEL_FALLBACK"] == ""
    assert "gemini-2.5-flash" not in WORKFLOW_PATH.read_text(encoding="utf-8")


def test_daily_analysis_exports_replay_price_history() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "python -m src.services.ai_snapshot_audit" in workflow_text
    assert "python -m src.services.ai_snapshot_replay_queue" in workflow_text
    assert "python -m src.services.ai_snapshot_price_history" in workflow_text
    assert "python -m src.services.ai_snapshot_gap_triage" in workflow_text
    assert "set -o pipefail" in workflow_text
    assert 'WORKFLOW_STEP_LOG="logs/stock_analysis_workflow_steps_$(date +%Y%m%d).log"' in workflow_text
    assert 'tee -a "$WORKFLOW_STEP_LOG"' in workflow_text
    assert "--queue-path reports/ai_snapshot/stock_ai_candidate_replay_queue_latest.jsonl" in workflow_text
    assert "--database-path data/stock_analysis.db" in workflow_text
    assert "--scope database" in workflow_text
    assert "--snapshot-audit-path reports/ai_snapshot/stock_ai_candidate_snapshot_audit_latest.json" in workflow_text


def test_daily_analysis_exports_run_log_audit() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["analyze"]["steps"]
    audit_step = next(
        (
            step
            for step in steps
            if "python -m src.services.daily_run_log_audit" in str(step.get("run", ""))
        ),
        None,
    )

    assert audit_step is not None
    assert audit_step.get("if") == "always()"
    assert audit_step["env"]["DEEPSEEK_API_KEY"] == "${{ secrets.DEEPSEEK_API_KEY }}"
    assert audit_step["env"]["DEEPSEEK_API_KEYS"] == "${{ secrets.DEEPSEEK_API_KEYS }}"
    assert audit_step["env"]["GEMINI_API_KEY"] == ""
    assert audit_step["env"]["GEMINI_API_KEYS"] == ""
    assert audit_step["env"]["GEMINI_MODEL"] == ""
    assert audit_step["env"]["GEMINI_MODEL_FALLBACK"] == ""
    assert audit_step["env"]["TUSHARE_TOKEN"] == "${{ secrets.TUSHARE_TOKEN }}"
    assert audit_step["env"]["TAVILY_API_KEYS"] == "${{ secrets.TAVILY_API_KEYS }}"
    assert audit_step["env"]["BOCHA_API_KEYS"] == "${{ secrets.BOCHA_API_KEYS }}"
    assert audit_step["env"]["LONGBRIDGE_ACCESS_TOKEN"] == "${{ secrets.LONGBRIDGE_ACCESS_TOKEN }}"
    assert "--log-dir logs" in audit_step["run"]
    assert "--output-dir reports/run_audit" in audit_step["run"]
    assert "daily_run_log_audit_latest.md" in audit_step["run"]
    assert "python -m src.services.ai_snapshot_step_summary" in audit_step["run"]
    assert "--input-dir reports/ai_snapshot" in audit_step["run"]
    assert "--output-file reports/run_audit/ai_snapshot_step_summary_latest.md" in audit_step["run"]
    assert "ai_snapshot_step_summary_latest.md" in audit_step["run"]
    assert "stock_ai_candidate_gap_triage_latest.md" in audit_step["run"]
    assert '>> "$GITHUB_STEP_SUMMARY"' in audit_step["run"]


def test_daily_analysis_tails_latest_stock_analysis_log() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "logs/stock_analysis_$(date +%Y%m%d).log" not in workflow_text
    assert "ls -t logs/stock_analysis_*.log" in workflow_text
    assert 'tail -30 "$latest_log"' in workflow_text


def test_network_smoke_uses_actions_runtime_env() -> None:
    for env in _load_network_smoke_envs():
        assert env["LITELLM_MODEL"] == (
            "${{ vars.LITELLM_MODEL || secrets.LITELLM_MODEL || 'deepseek/deepseek-v4-pro' }}"
        )
        assert env["LITELLM_FALLBACK_MODELS"] == (
            "${{ vars.LITELLM_FALLBACK_MODELS || secrets.LITELLM_FALLBACK_MODELS || 'deepseek/deepseek-v4-pro' }}"
        )
        assert env["LITELLM_TIMEOUT_SECONDS"] == (
            "${{ vars.LITELLM_TIMEOUT_SECONDS || secrets.LITELLM_TIMEOUT_SECONDS || '90' }}"
        )
        assert env["DEEPSEEK_API_KEY"] == "${{ secrets.DEEPSEEK_API_KEY }}"
        assert env["DEEPSEEK_API_KEYS"] == "${{ secrets.DEEPSEEK_API_KEYS }}"
        assert env["LLM_DEEPSEEK_API_KEY"] == "${{ secrets.LLM_DEEPSEEK_API_KEY }}"
        assert env["LLM_DEEPSEEK_API_KEYS"] == "${{ secrets.LLM_DEEPSEEK_API_KEYS }}"
        assert env["GEMINI_API_KEY"] == ""
        assert env["GEMINI_API_KEYS"] == ""
        assert env["TUSHARE_TOKEN"] == "${{ secrets.TUSHARE_TOKEN }}"
        assert env["LONGBRIDGE_APP_KEY"] == "${{ secrets.LONGBRIDGE_APP_KEY }}"
        assert env["LONGBRIDGE_APP_SECRET"] == "${{ secrets.LONGBRIDGE_APP_SECRET }}"
        assert env["LONGBRIDGE_ACCESS_TOKEN"] == "${{ secrets.LONGBRIDGE_ACCESS_TOKEN }}"
        assert env["TAVILY_API_KEYS"] == "${{ secrets.TAVILY_API_KEYS }}"
        assert env["BOCHA_API_KEYS"] == "${{ secrets.BOCHA_API_KEYS }}"
        assert env["BRAVE_API_KEYS"] == "${{ secrets.BRAVE_API_KEYS }}"
        assert env["SERPAPI_API_KEYS"] == "${{ secrets.SERPAPI_API_KEYS }}"
        assert env["ANSPIRE_API_KEYS"] == "${{ secrets.ANSPIRE_API_KEYS }}"
        assert env["MINIMAX_API_KEYS"] == "${{ secrets.MINIMAX_API_KEYS }}"
        assert env["SEARXNG_BASE_URLS"] == "${{ secrets.SEARXNG_BASE_URLS }}"
        assert env["REALTIME_SOURCE_PRIORITY"] == (
            "${{ vars.REALTIME_SOURCE_PRIORITY || 'tushare,tencent,akshare_sina,efinance,akshare_em' }}"
        )
        assert env["SEARXNG_PUBLIC_INSTANCES_ENABLED"] == (
            "${{ vars.SEARXNG_PUBLIC_INSTANCES_ENABLED || secrets.SEARXNG_PUBLIC_INSTANCES_ENABLED || 'true' }}"
        )


def test_network_smoke_steps_have_hard_timeouts() -> None:
    steps_by_name = {step["name"]: step for step in _load_network_smoke_steps()}

    assert steps_by_name["Run pytest network smoke (non-blocking)"]["timeout-minutes"] == 8
    assert steps_by_name["Run quick smoke (non-blocking)"]["timeout-minutes"] == 10


def test_network_smoke_writes_exit_status_and_audit_artifacts() -> None:
    workflow_text = NETWORK_SMOKE_PATH.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    steps = workflow["jobs"]["smoke"]["steps"]
    audit_step = next(
        (
            step
            for step in steps
            if "python -m src.services.network_smoke_audit" in str(step.get("run", ""))
        ),
        None,
    )

    assert "pytest-network.status" in workflow_text
    assert "quick-smoke.status" in workflow_text
    assert "${PIPESTATUS[0]}" in workflow_text
    assert audit_step is not None
    assert audit_step.get("if") == "always()"
    assert audit_step["env"]["DEEPSEEK_API_KEY"] == "${{ secrets.DEEPSEEK_API_KEY }}"
    assert audit_step["env"]["TUSHARE_TOKEN"] == "${{ secrets.TUSHARE_TOKEN }}"
    assert audit_step["env"]["TAVILY_API_KEYS"] == "${{ secrets.TAVILY_API_KEYS }}"
    assert audit_step["env"]["BOCHA_API_KEYS"] == "${{ secrets.BOCHA_API_KEYS }}"
    assert audit_step["env"]["LONGBRIDGE_ACCESS_TOKEN"] == "${{ secrets.LONGBRIDGE_ACCESS_TOKEN }}"
    assert "--output-dir reports/network_smoke" in audit_step["run"]
    assert "network_smoke_audit_latest.md" in audit_step["run"]
    assert '>> "$GITHUB_STEP_SUMMARY"' in audit_step["run"]
    assert "reports/network_smoke/" in workflow_text


def test_env_example_includes_provider_template_channel_examples() -> None:
    templates = _extract_provider_templates()
    env_example = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    for channel, base_url in templates.items():
        upper = channel.upper()
        assert f"LLM_CHANNELS={channel}" in env_example
        assert f"LLM_{upper}_MODELS=" in env_example

        if channel != "ollama":
            assert f"LLM_{upper}_API_KEY=" in env_example
        if base_url:
            assert f"LLM_{upper}_BASE_URL=" in env_example
        if channel != "ollama":
            assert f"LLM_{upper}_PROTOCOL=" in env_example

    assert "LLM_CHANNELS=ark" not in env_example
    assert "LLM_ARK_" not in env_example
