# -*- coding: utf-8 -*-
"""Static checks for the optional AI replay validation workflow."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT_DIR / ".github/workflows/ai-replay-validation.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _dispatch_inputs(workflow: dict) -> dict:
    on_block = workflow.get("on") or workflow.get(True)
    return on_block["workflow_dispatch"]["inputs"]


def test_ai_replay_validation_workflow_has_manual_inputs_and_read_permissions() -> None:
    workflow = _workflow()
    inputs = _dispatch_inputs(workflow)
    on_block = workflow.get("on") or workflow.get(True)
    job = workflow["jobs"]["replay-validation"]

    assert on_block["workflow_run"]["workflows"] == ["每日股票分析"]
    assert on_block["workflow_run"]["types"] == ["completed"]
    assert inputs["source_run_id"]["required"] is True
    assert inputs["analysis_artifact_name"]["default"] == ""
    assert inputs["baseline_run_id"]["default"] == ""
    assert inputs["baseline_artifact_name"]["default"] == ""
    assert inputs["price_source_run_id"]["default"] == ""
    assert inputs["price_artifact_name"]["default"] == ""
    assert inputs["price_file"]["default"] == "reports/ai_snapshot/stock_ai_candidate_price_history_latest.csv"
    assert job["if"] == "${{ github.event_name != 'workflow_run' || github.event.workflow_run.conclusion == 'success' }}"
    assert job["permissions"] == {"actions": "read", "contents": "read"}


def test_ai_replay_validation_workflow_runs_replay_tools_and_uploads_outputs() -> None:
    workflow = _workflow()
    steps = {step["name"]: step for step in workflow["jobs"]["replay-validation"]["steps"]}
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "Download analysis artifact" in steps
    assert "Resolve replay inputs" in steps
    assert "Download baseline artifact" in steps
    assert "Validate snapshot and replay queue" in steps
    assert "Resolve replay outcomes" in steps
    assert "Upload replay validation artifact" in steps

    assert 'gh run download "$SOURCE_RUN_ID"' in text
    assert "Auto-selected baseline_run_id" in text
    assert "actions/workflows/00-daily-analysis.yml/runs?status=success" in text
    assert "No price artifact or price_source_run_id provided; skipping replay outcome resolution." in text
    assert "must be a numeric GitHub Actions run id" in text
    assert "python -m src.services.ai_snapshot_audit" in text
    assert "python -m src.services.ai_snapshot_replay_queue" in text
    assert "python -m src.services.ai_snapshot_turnover" in text
    assert "python -m src.services.ai_snapshot_replay_outcome" in text
    assert "### Audit Status" in text
    assert "### Candidate Turnover" in text
    assert "### Guard Policy Candidates" in text
    assert "### Case Tag Outcomes" in text
    assert "stock_ai_candidate_snapshot_audit_latest.json" in text
    assert "stock_ai_candidate_replay_queue_audit_latest.json" in text
    assert "stock_ai_candidate_turnover_audit_latest.json" in text
    assert "stock_ai_candidate_turnover_latest.json" in text
    assert "stock_ai_candidate_price_history_audit_latest.json" in text
    assert "Price history audit" in text
    assert "stock_ai_candidate_replay_outcome_audit_latest.json" in text
    assert "stock_ai_candidate_replay_guard_policy_latest.json" in text
    assert "stock_ai_candidate_replay_case_tag_summary_latest.json" in text
    assert "flow_broke_proxy" in text
    assert "Net Edge" in text
    assert "cost_buffer_status" in text
    assert 'cp "$price_audit_path" replay-validation/stock_ai_candidate_price_history_audit_latest.json' in text
    assert "--no-fail" not in text
    assert "head -n 1" not in text
    assert "[.workflow_runs[] | select(" in text
    assert "[.artifacts[] | select(" in text
    assert "price_source_run_id" in text
    assert "baseline_run_id" in text
    assert "baseline_artifact_name" in text
    assert "price_artifact_name" in text
    assert "stock_ai_candidate_price_history_latest.csv" in text
    assert "if" not in steps["Download baseline artifact"]
    assert "if" not in steps["Resolve replay outcomes"]
    assert steps["Upload replay validation artifact"]["with"]["name"] == "ai-replay-validation-${{ github.run_number }}"


def test_ai_replay_validation_workflow_does_not_require_provider_secrets() -> None:
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "secrets." not in text
    assert "GH_TOKEN: ${{ github.token }}" in text
