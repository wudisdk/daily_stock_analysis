# -*- coding: utf-8 -*-
"""Regression tests for Analyzer LLM availability checks."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.analyzer import GeminiAnalyzer


def _config(**overrides):
    data = {
        "litellm_model": "deepseek/deepseek-v4-flash",
        "litellm_fallback_models": ["deepseek/deepseek-v4-pro"],
        "llm_model_list": [],
        "gemini_api_keys": [],
        "anthropic_api_keys": [],
        "deepseek_api_keys": [],
        "openai_api_keys": [],
        "openai_base_url": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_analyzer_unavailable_for_deepseek_model_without_key() -> None:
    analyzer = GeminiAnalyzer(config=_config())

    assert analyzer.is_available() is False

    with patch("src.analyzer.litellm.completion") as completion:
        assert analyzer.generate_text("hello") is None

    completion.assert_not_called()


def test_analyzer_available_for_deepseek_model_with_key() -> None:
    analyzer = GeminiAnalyzer(config=_config(deepseek_api_keys=["sk-test-value"]))

    assert analyzer.is_available() is True
