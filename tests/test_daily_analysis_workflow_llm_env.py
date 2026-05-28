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
        "${{ vars.LITELLM_MODEL || secrets.LITELLM_MODEL || 'deepseek/deepseek-v4-flash' }}"
    )
    assert env["LITELLM_FALLBACK_MODELS"] == (
        "${{ vars.LITELLM_FALLBACK_MODELS || secrets.LITELLM_FALLBACK_MODELS || 'deepseek/deepseek-v4-pro' }}"
    )
    assert env["LITELLM_LOG_LEVEL"] == (
        "${{ vars.LITELLM_LOG_LEVEL || secrets.LITELLM_LOG_LEVEL || 'ERROR' }}"
    )
    assert env["SEARXNG_PUBLIC_INSTANCES_ENABLED"] == (
        "${{ vars.SEARXNG_PUBLIC_INSTANCES_ENABLED || secrets.SEARXNG_PUBLIC_INSTANCES_ENABLED || 'false' }}"
    )
    assert env["REALTIME_SOURCE_PRIORITY"] == (
        "${{ vars.REALTIME_SOURCE_PRIORITY || 'tushare,tencent,akshare_sina,efinance,akshare_em' }}"
    )


def test_network_smoke_uses_actions_runtime_env() -> None:
    for env in _load_network_smoke_envs():
        assert env["LITELLM_MODEL"] == (
            "${{ vars.LITELLM_MODEL || secrets.LITELLM_MODEL || 'deepseek/deepseek-v4-flash' }}"
        )
        assert env["LITELLM_FALLBACK_MODELS"] == (
            "${{ vars.LITELLM_FALLBACK_MODELS || secrets.LITELLM_FALLBACK_MODELS || 'deepseek/deepseek-v4-pro' }}"
        )
        assert env["DEEPSEEK_API_KEY"] == "${{ secrets.DEEPSEEK_API_KEY }}"
        assert env["TUSHARE_TOKEN"] == "${{ secrets.TUSHARE_TOKEN }}"
        assert env["TAVILY_API_KEYS"] == "${{ secrets.TAVILY_API_KEYS }}"
        assert env["REALTIME_SOURCE_PRIORITY"] == (
            "${{ vars.REALTIME_SOURCE_PRIORITY || 'tushare,tencent,akshare_sina,efinance,akshare_em' }}"
        )
        assert env["SEARXNG_PUBLIC_INSTANCES_ENABLED"] == (
            "${{ vars.SEARXNG_PUBLIC_INSTANCES_ENABLED || secrets.SEARXNG_PUBLIC_INSTANCES_ENABLED || 'false' }}"
        )


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
