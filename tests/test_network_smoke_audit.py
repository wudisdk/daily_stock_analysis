# -*- coding: utf-8 -*-
"""Tests for network smoke audit artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.services.network_smoke_audit import audit_network_smoke, write_network_smoke_audit_outputs


def _source(tmp_path: Path, name: str) -> tuple[str, Path, Path]:
    return name, tmp_path / f"{name}.log", tmp_path / f"{name}.status"


def _provider_env() -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": "sk-" + "deepseek-placeholder-123456",
        "TUSHARE_TOKEN": "tushare-token",
        "TAVILY_API_KEYS": "tvly-placeholder-123456",
    }


def test_audit_network_smoke_passes_clean_logs(tmp_path: Path) -> None:
    pytest_source = _source(tmp_path, "pytest-network")
    quick_source = _source(tmp_path, "quick-smoke")
    pytest_source[1].write_text("2 passed, 1 skipped in 3.0s\n", encoding="utf-8")
    pytest_source[2].write_text("exit_code=0\n", encoding="utf-8")
    quick_source[1].write_text("quick smoke ok\n", encoding="utf-8")
    quick_source[2].write_text("exit_code=0\n", encoding="utf-8")

    audit = audit_network_smoke(
        sources=[pytest_source, quick_source],
        created_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
        environ=_provider_env(),
    )

    assert audit["overall_status"] == "PASS"
    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["network_smoke_exit_codes"]["status"] == "PASS"
    assert checks["pytest_network_summary"]["status"] == "PASS"
    assert checks["provider_env_deepseek"]["status"] == "PASS"
    assert checks["provider_env_market_data"]["status"] == "PASS"
    assert checks["provider_env_search"]["status"] == "PASS"
    assert audit["summary"]["provider_env"]["configured_providers"] == ["deepseek", "tushare", "tavily"]


def test_audit_network_smoke_fails_failed_exit_and_pytest_summary(tmp_path: Path) -> None:
    pytest_source = _source(tmp_path, "pytest-network")
    quick_source = _source(tmp_path, "quick-smoke")
    pytest_source[1].write_text("1 failed, 1 passed in 3.0s\nFAILED tests/test_live.py\n", encoding="utf-8")
    pytest_source[2].write_text("exit_code=1\n", encoding="utf-8")
    quick_source[1].write_text("quick smoke ok\n", encoding="utf-8")
    quick_source[2].write_text("exit_code=0\n", encoding="utf-8")

    audit = audit_network_smoke(sources=[pytest_source, quick_source], environ=_provider_env())

    assert audit["overall_status"] == "FAIL"
    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["network_smoke_exit_codes"]["status"] == "FAIL"
    assert checks["pytest_network_summary"]["status"] == "FAIL"


def test_audit_network_smoke_warns_quota_and_network_signals(tmp_path: Path) -> None:
    pytest_source = _source(tmp_path, "pytest-network")
    quick_source = _source(tmp_path, "quick-smoke")
    pytest_source[1].write_text(
        "2 passed in 3.0s\nTavily request exceeds your plan usage limit\n",
        encoding="utf-8",
    )
    pytest_source[2].write_text("exit_code=0\n", encoding="utf-8")
    quick_source[1].write_text("network timeout while checking data provider\n", encoding="utf-8")
    quick_source[2].write_text("exit_code=0\n", encoding="utf-8")

    audit = audit_network_smoke(sources=[pytest_source, quick_source], environ=_provider_env())

    assert audit["overall_status"] == "WARN"
    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["quota_rate_limit_signals"]["status"] == "WARN"
    assert checks["network_error_signals"]["status"] == "WARN"


def test_audit_network_smoke_fails_secret_like_value_without_copying_secret(tmp_path: Path) -> None:
    secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
    pytest_source = _source(tmp_path, "pytest-network")
    quick_source = _source(tmp_path, "quick-smoke")
    pytest_source[1].write_text(f"2 passed in 3.0s\n{secret}\n", encoding="utf-8")
    pytest_source[2].write_text("exit_code=0\n", encoding="utf-8")
    quick_source[1].write_text("quick smoke ok\n", encoding="utf-8")
    quick_source[2].write_text("exit_code=0\n", encoding="utf-8")

    audit = audit_network_smoke(sources=[pytest_source, quick_source], environ=_provider_env())
    output_dir = tmp_path / "out"
    write_network_smoke_audit_outputs(audit, output_dir)

    assert audit["overall_status"] == "FAIL"
    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["secret_like_values"]["status"] == "FAIL"
    for path in output_dir.iterdir():
        assert secret not in path.read_text(encoding="utf-8-sig")


def test_audit_network_smoke_ignores_news_urls_with_sk_slugs(tmp_path: Path) -> None:
    pytest_source = _source(tmp_path, "pytest-network")
    quick_source = _source(tmp_path, "quick-smoke")
    pytest_source[1].write_text(
        "2 passed in 3.0s\nhttps://www.cnbc.com/2026/05/27/sk-hynix-ai-chip-news.html\n",
        encoding="utf-8",
    )
    pytest_source[2].write_text("exit_code=0\n", encoding="utf-8")
    quick_source[1].write_text("quick smoke ok\n", encoding="utf-8")
    quick_source[2].write_text("exit_code=0\n", encoding="utf-8")

    audit = audit_network_smoke(sources=[pytest_source, quick_source], environ=_provider_env())

    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["secret_like_values"]["status"] == "PASS"
    assert audit["summary"]["secret_like_value_count"] == 0


def test_audit_network_smoke_warns_missing_and_partial_provider_env(tmp_path: Path) -> None:
    pytest_source = _source(tmp_path, "pytest-network")
    quick_source = _source(tmp_path, "quick-smoke")
    pytest_source[1].write_text("2 passed in 3.0s\n", encoding="utf-8")
    pytest_source[2].write_text("exit_code=0\n", encoding="utf-8")
    quick_source[1].write_text("quick smoke ok\n", encoding="utf-8")
    quick_source[2].write_text("exit_code=0\n", encoding="utf-8")

    audit = audit_network_smoke(
        sources=[pytest_source, quick_source],
        environ={
            "LONGBRIDGE_APP_KEY": "lb-app-key",
            "TAVILY_API_KEYS": "tvly-placeholder-1,tvly-placeholder-2",
        },
    )

    assert audit["overall_status"] == "WARN"
    checks = {check["check"]: check for check in audit["checks"]}
    assert checks["provider_env_deepseek"]["status"] == "WARN"
    assert checks["provider_env_market_data"]["status"] == "WARN"
    assert checks["provider_env_search"]["status"] == "PASS"
    assert checks["provider_env_partial_credentials"]["status"] == "WARN"
    assert audit["summary"]["provider_env"]["partial_providers"] == ["longbridge"]


def test_write_network_smoke_audit_outputs_writes_latest_and_dated_files(tmp_path: Path) -> None:
    audit = {
        "generated_at": "2026-05-29T00:00:00+00:00",
        "overall_status": "PASS",
        "status_counts": {"PASS": 1},
        "summary": {"failed_sources": [], "quota_signal_count": 0, "network_error_signal_count": 0},
        "checks": [
            {
                "check": "network_smoke_exit_codes",
                "status": "PASS",
                "message": "ok",
                "evidence": {"failed": []},
            }
        ],
    }

    paths = write_network_smoke_audit_outputs(audit, tmp_path)
    names = {path.name for path in paths}

    assert names == {
        "network_smoke_audit_latest.json",
        "network_smoke_audit_20260529.json",
        "network_smoke_audit_latest.csv",
        "network_smoke_audit_20260529.csv",
        "network_smoke_audit_latest.md",
        "network_smoke_audit_20260529.md",
    }
    assert json.loads((tmp_path / "network_smoke_audit_latest.json").read_text(encoding="utf-8"))[
        "overall_status"
    ] == "PASS"
    assert "network_smoke_exit_codes" in (tmp_path / "network_smoke_audit_latest.csv").read_text(
        encoding="utf-8-sig"
    )
