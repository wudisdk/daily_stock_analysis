# -*- coding: utf-8 -*-
"""Tests for replay queue outcome resolution."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

from src.services.ai_snapshot_replay_outcome import (
    audit_replay_outcomes,
    build_replay_outcome_rows,
    main as replay_outcome_main,
    summarize_replay_case_tag_outcomes,
    summarize_replay_dimension_outcomes,
    summarize_replay_guard_policy_candidates,
    summarize_replay_invalidation_effects,
    summarize_replay_invalidation_outcomes,
    summarize_replay_model_outcomes,
    summarize_replay_outcomes,
    write_replay_outcome_outputs,
)
from src.services.ai_snapshot_replay_queue import build_replay_queue_rows


def _snapshot_row(
    code: str,
    digest: str,
    trade_date: str = "2026-05-28",
    *,
    de_risk_label: str = "none",
    decision_type: str = "hold",
    sentiment_score: int = 70,
    confidence_level: str = "medium",
) -> dict:
    return {
        "schema_version": "1.1",
        "snapshot_kind": "post_analysis_candidate",
        "candidate_source": "daily_analysis",
        "candidate_rank": 1,
        "trade_date": trade_date,
        "stock_code": code,
        "stock_name": code,
        "model_used": "deepseek/deepseek-v4-pro",
        "model_result": {
            "decision_type": decision_type,
            "sentiment_score": sentiment_score,
            "confidence_level": confidence_level,
        },
        "input_snapshot_hash": digest,
        "factor_snapshot": {
            "dimensions": [
                {"name": "price_heat", "status": "available", "label": "normal"},
                {"name": "fund_flow", "status": "available", "label": "supportive"},
                {"name": "de_risk", "status": "available", "label": de_risk_label},
                {"name": "risk", "status": "available", "label": "no_risk_flags"},
                {"name": "data_coverage", "status": "available", "label": "high"},
            ],
        },
    }


def _price_rows() -> list[dict]:
    rows = []
    for code, closes in {
        "688981": [100, 101, 102, 103, 104, 105, 106],
        "HK00981": [200, 198, 202, 206, 204, 210, 212],
        "BENCH": [50, 51, 50, 52, 53, 54, 55],
    }.items():
        for idx, close in enumerate(closes, start=28):
            rows.append({"stock_code": code, "trade_date": f"2026-05-{idx:02d}", "close": close})
    return rows


def _write_jsonl(path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_price_csv(path, rows) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["stock_code", "trade_date", "close"])
        writer.writeheader()
        writer.writerows(rows)


def test_build_replay_outcome_rows_resolves_future_and_benchmark_returns() -> None:
    queue_rows = build_replay_queue_rows(
        [_snapshot_row("688981", "a" * 64), _snapshot_row("HK00981", "b" * 64)],
        generated_at=datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc),
    )

    outcomes = build_replay_outcome_rows(
        queue_rows,
        _price_rows(),
        generated_at=datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc),
    )

    first_1d = next(row for row in outcomes if row["stock_code"] == "688981" and row["eval_horizon"] == "1d")
    assert first_1d["outcome_status"] == "available"
    assert first_1d["anchor_trade_date"] == "2026-05-28"
    assert first_1d["future_trade_date"] == "2026-05-29"
    assert first_1d["future_return"] == 0.01
    assert first_1d["benchmark_return"] == 0.0066666667
    assert first_1d["excess_return"] == 0.0033333333
    assert first_1d["outcome_join_key"] == f"{'a' * 64}:1d"

    one_month = next(row for row in outcomes if row["stock_code"] == "688981" and row["eval_horizon"] == "1m")
    assert one_month["outcome_status"] == "pending_future_price"
    assert "future_return" not in one_month


def test_summarize_replay_outcomes_groups_by_horizon_model_status() -> None:
    queue_rows = build_replay_queue_rows([_snapshot_row("688981", "a" * 64)])
    outcomes = build_replay_outcome_rows(queue_rows, _price_rows())

    summary = summarize_replay_outcomes(outcomes)

    available_1d = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d" and row["outcome_status"] == "available"
    )
    assert available_1d["available_count"] == 1
    assert available_1d["available_ratio"] == 1.0
    assert available_1d["win_rate"] == 1.0

    pending_1m = next(
        row
        for row in summary
        if row["eval_horizon"] == "1m" and row["outcome_status"] == "pending_future_price"
    )
    assert pending_1m["available_count"] == 0
    assert "win_rate" not in pending_1m


def test_summarize_replay_invalidation_outcomes_compares_flag_hit_and_miss() -> None:
    queue_rows = build_replay_queue_rows(
        [
            _snapshot_row("688981", "a" * 64, de_risk_label="flow_broke_price_flow_hot"),
            _snapshot_row("HK00981", "b" * 64),
        ]
    )
    outcomes = build_replay_outcome_rows(queue_rows, _price_rows())

    summary = summarize_replay_invalidation_outcomes(outcomes)

    flow_hit = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["invalidation_flag"] == "flow_broke_proxy"
        and row["flag_state"] == "hit"
    )
    flow_miss = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["invalidation_flag"] == "flow_broke_proxy"
        and row["flag_state"] == "miss"
    )
    hot_hit = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["invalidation_flag"] == "price_flow_hot_proxy"
        and row["flag_state"] == "hit"
    )

    assert flow_hit["n_rows"] == 1
    assert flow_hit["avg_future_return"] == 0.01
    assert flow_miss["n_rows"] == 1
    assert flow_miss["avg_future_return"] == -0.01
    assert hot_hit["n_rows"] == 1


def test_summarize_replay_invalidation_effects_compares_hit_vs_miss_directly() -> None:
    queue_rows = build_replay_queue_rows(
        [
            _snapshot_row("688981", "a" * 64, de_risk_label="flow_broke_price_flow_hot"),
            _snapshot_row("HK00981", "b" * 64),
        ]
    )
    outcomes = build_replay_outcome_rows(queue_rows, _price_rows())

    effects = summarize_replay_invalidation_effects(outcomes)

    flow_1d = next(
        row
        for row in effects
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["invalidation_flag"] == "flow_broke_proxy"
    )
    assert flow_1d["hit_available_count"] == 1
    assert flow_1d["miss_available_count"] == 1
    assert flow_1d["hit_avg_future_return"] == 0.01
    assert flow_1d["miss_avg_future_return"] == -0.01
    assert flow_1d["delta_avg_future_return"] == 0.02
    assert flow_1d["delta_avg_excess_return"] == 0.02
    assert flow_1d["effect_direction"] == "hit_outperformed_miss"
    assert flow_1d["evidence_status"] == "paired_available"


def test_summarize_replay_guard_policy_candidates_marks_dynamic_deweight_rule() -> None:
    rows = []
    for idx in range(6):
        rows.append(
            {
                "stock_code": f"HIT{idx}",
                "eval_horizon": "1m",
                "model_used": "deepseek/deepseek-v4-pro",
                "outcome_status": "available",
                "invalidation_flags": ["flow_broke_proxy"],
                "future_return": -0.02,
                "excess_return": -0.03,
            }
        )
        rows.append(
            {
                "stock_code": f"MISS{idx}",
                "eval_horizon": "1m",
                "model_used": "deepseek/deepseek-v4-pro",
                "outcome_status": "available",
                "invalidation_flags": [],
                "future_return": 0.03,
                "excess_return": 0.02,
            }
        )

    candidates = summarize_replay_guard_policy_candidates(rows)

    flow = next(row for row in candidates if row["invalidation_flag"] == "flow_broke_proxy")
    assert flow["hit_available_count"] == 6
    assert flow["miss_available_count"] == 6
    assert flow["delta_avg_excess_return"] == -0.05
    assert flow["assumed_round_trip_cost_bps"] == 30
    assert flow["assumed_round_trip_cost_rate"] == 0.003
    assert flow["gross_deweight_edge"] == 0.05
    assert flow["net_deweight_edge_after_cost"] == 0.047
    assert flow["cost_buffer_status"] == "material_after_cost"
    assert flow["directional_hint"] == "flag_hit_worse"
    assert flow["evidence_strength"] == "usable"
    assert flow["policy_signal"] == "exit_deweight_candidate"
    assert flow["recommended_action"] == "backtest_dynamic_exit_or_deweight"


def test_summarize_replay_guard_policy_candidates_applies_cost_buffer() -> None:
    rows = []
    for idx in range(6):
        rows.append(
            {
                "stock_code": f"HIT{idx}",
                "eval_horizon": "1m",
                "model_used": "deepseek/deepseek-v4-pro",
                "outcome_status": "available",
                "invalidation_flags": ["flow_broke_proxy"],
                "future_return": 0.0,
                "excess_return": -0.001,
            }
        )
        rows.append(
            {
                "stock_code": f"MISS{idx}",
                "eval_horizon": "1m",
                "model_used": "deepseek/deepseek-v4-pro",
                "outcome_status": "available",
                "invalidation_flags": [],
                "future_return": 0.0,
                "excess_return": 0.001,
            }
        )

    candidates = summarize_replay_guard_policy_candidates(rows)

    flow = next(row for row in candidates if row["invalidation_flag"] == "flow_broke_proxy")
    assert flow["delta_avg_excess_return"] == -0.002
    assert flow["gross_deweight_edge"] == 0.002
    assert flow["net_deweight_edge_after_cost"] == -0.001
    assert flow["cost_buffer_status"] == "not_cost_covered"
    assert flow["policy_signal"] == "neutral_edge"
    assert flow["recommended_action"] == "keep_observing"


def test_summarize_replay_dimension_outcomes_groups_low_sensitivity_labels() -> None:
    queue_rows = build_replay_queue_rows(
        [
            _snapshot_row("688981", "a" * 64, de_risk_label="flow_broke_price_flow_hot"),
            _snapshot_row("HK00981", "b" * 64),
        ]
    )
    outcomes = build_replay_outcome_rows(queue_rows, _price_rows())

    summary = summarize_replay_dimension_outcomes(outcomes)

    de_risk_hot = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["dimension_name"] == "de_risk"
        and row["dimension_label"] == "flow_broke_price_flow_hot"
    )
    fund_flow = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["dimension_name"] == "fund_flow"
        and row["dimension_label"] == "supportive"
    )
    assert de_risk_hot["n_rows"] == 1
    assert de_risk_hot["avg_future_return"] == 0.01
    assert fund_flow["n_rows"] == 2
    assert fund_flow["available_count"] == 2


def test_summarize_replay_case_tag_outcomes_groups_low_sensitivity_tags() -> None:
    queue_rows = build_replay_queue_rows(
        [
            _snapshot_row("688981", "a" * 64, de_risk_label="flow_broke_price_flow_hot"),
            _snapshot_row("HK00981", "b" * 64),
        ]
    )
    outcomes = build_replay_outcome_rows(queue_rows, _price_rows())

    summary = summarize_replay_case_tag_outcomes(outcomes)

    flow = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["case_tag"] == "flow_broke"
    )
    fund = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["case_tag"] == "fund_flow_available"
    )
    assert flow["case_tag_group"] == "bear"
    assert flow["n_rows"] == 1
    assert flow["avg_future_return"] == 0.01
    assert fund["case_tag_group"] == "bull"
    assert fund["n_rows"] == 2
    assert fund["available_count"] == 2


def test_summarize_replay_model_outcomes_groups_decision_and_score_bucket() -> None:
    queue_rows = build_replay_queue_rows(
        [
            _snapshot_row("688981", "a" * 64, decision_type="buy", sentiment_score=82, confidence_level="high"),
            _snapshot_row("HK00981", "b" * 64, decision_type="hold", sentiment_score=55, confidence_level="medium"),
        ]
    )
    outcomes = build_replay_outcome_rows(queue_rows, _price_rows())

    summary = summarize_replay_model_outcomes(outcomes)

    buy_1d = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["model_decision_type"] == "buy"
        and row["model_sentiment_bucket"] == "score_80_plus"
        and row["model_confidence_level"] == "high"
    )
    hold_1d = next(
        row
        for row in summary
        if row["eval_horizon"] == "1d"
        and row["outcome_status"] == "available"
        and row["model_decision_type"] == "hold"
        and row["model_sentiment_bucket"] == "score_50_59"
    )
    assert buy_1d["n_rows"] == 1
    assert buy_1d["avg_future_return"] == 0.01
    assert hold_1d["n_rows"] == 1
    assert hold_1d["avg_future_return"] == -0.01


def test_audit_replay_outcomes_passes_available_rows_and_summaries() -> None:
    queue_rows = build_replay_queue_rows(
        [
            _snapshot_row("688981", "a" * 64, de_risk_label="flow_broke_price_flow_hot"),
            _snapshot_row("HK00981", "b" * 64),
        ]
    )
    outcomes = build_replay_outcome_rows(queue_rows, _price_rows())

    audit = audit_replay_outcomes(outcomes)
    checks = {check["check"]: check for check in audit["checks"]}

    assert audit["overall_status"] == "PASS"
    assert checks["replay_outcome_future_price_availability"]["status"] == "PASS"
    assert checks["replay_model_summary_visible"]["status"] == "PASS"
    assert checks["replay_dimension_summary_visible"]["status"] == "PASS"
    assert checks["replay_case_tag_summary_visible"]["status"] == "PASS"
    assert checks["replay_invalidation_summary_visible"]["status"] == "PASS"
    assert checks["replay_outcome_low_sensitivity_boundary"]["status"] == "PASS"


def test_audit_replay_outcomes_warns_when_waiting_for_future_prices() -> None:
    queue_rows = build_replay_queue_rows([_snapshot_row("688981", "a" * 64)])
    outcomes = build_replay_outcome_rows(
        queue_rows,
        [{"stock_code": "688981", "trade_date": "2026-05-28", "close": 100.0}],
    )

    audit = audit_replay_outcomes(outcomes)
    checks = {check["check"]: check for check in audit["checks"]}

    assert audit["overall_status"] == "WARN"
    assert checks["replay_outcome_future_price_availability"]["status"] == "WARN"


def test_audit_replay_outcomes_fails_join_errors_and_sensitive_values() -> None:
    rows = [
        {
            "outcome_schema_version": "1.2",
            "generated_at": "2026-05-29T00:00:00+00:00",
            "outcome_join_key": "bad:1d",
            "input_snapshot_hash": "bad",
            "stock_code": "688981",
            "trade_date": "2026-05-28",
            "eval_horizon": "1d",
            "forward_trading_days": 1,
            "model_used": "deepseek/deepseek-v4-pro",
            "model_operation_advice": "sk-test-sensitive-value-123456",
            "outcome_status": "missing_anchor_price",
        }
    ]

    audit = audit_replay_outcomes(rows)
    checks = {check["check"]: check for check in audit["checks"]}

    assert audit["overall_status"] == "FAIL"
    assert checks["replay_outcome_future_price_availability"]["status"] == "FAIL"
    assert checks["replay_outcome_low_sensitivity_boundary"]["status"] == "FAIL"


def test_build_replay_outcome_rows_resolves_stock_code_aliases() -> None:
    queue_rows = build_replay_queue_rows([_snapshot_row("600519.SH", "c" * 64)])
    price_rows = [
        {"stock_code": "600519", "trade_date": "2026-05-28", "close": 100.0},
        {"stock_code": "600519", "trade_date": "2026-05-29", "close": 102.0},
        {"stock_code": "000001.SZ", "trade_date": "2026-05-28", "close": 10.0},
        {"stock_code": "000001.SZ", "trade_date": "2026-05-29", "close": 10.1},
    ]

    outcomes = build_replay_outcome_rows(queue_rows, price_rows)

    first_1d = next(row for row in outcomes if row["eval_horizon"] == "1d")
    assert first_1d["outcome_status"] == "available"
    assert first_1d["future_return"] == 0.02
    assert first_1d["benchmark_return"] == 0.015


def test_write_replay_outcome_outputs_writes_latest_and_dated_files(tmp_path) -> None:
    queue_path = tmp_path / "stock_ai_candidate_replay_queue_latest.jsonl"
    price_path = tmp_path / "prices.csv"
    queue_rows = build_replay_queue_rows(
        [
            _snapshot_row("688981", "a" * 64, de_risk_label="flow_broke_price_flow_hot"),
            _snapshot_row("HK00981", "b" * 64),
        ]
    )
    _write_jsonl(queue_path, queue_rows)
    _write_price_csv(price_path, _price_rows())

    paths = write_replay_outcome_outputs(
        queue_path,
        price_path,
        output_dir=tmp_path,
        generated_at=datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc),
    )

    assert {path.name for path in paths} == {
        "stock_ai_candidate_replay_outcome_latest.jsonl",
        "stock_ai_candidate_replay_outcome_20260528.jsonl",
        "stock_ai_candidate_replay_outcome_latest.csv",
        "stock_ai_candidate_replay_outcome_20260528.csv",
        "stock_ai_candidate_replay_outcome_summary_latest.json",
        "stock_ai_candidate_replay_outcome_summary_20260528.json",
        "stock_ai_candidate_replay_outcome_summary_latest.csv",
        "stock_ai_candidate_replay_outcome_summary_20260528.csv",
        "stock_ai_candidate_replay_invalidation_summary_latest.json",
        "stock_ai_candidate_replay_invalidation_summary_20260528.json",
        "stock_ai_candidate_replay_invalidation_summary_latest.csv",
        "stock_ai_candidate_replay_invalidation_summary_20260528.csv",
        "stock_ai_candidate_replay_invalidation_effect_latest.json",
        "stock_ai_candidate_replay_invalidation_effect_20260528.json",
        "stock_ai_candidate_replay_invalidation_effect_latest.csv",
        "stock_ai_candidate_replay_invalidation_effect_20260528.csv",
        "stock_ai_candidate_replay_guard_policy_latest.json",
        "stock_ai_candidate_replay_guard_policy_20260528.json",
        "stock_ai_candidate_replay_guard_policy_latest.csv",
        "stock_ai_candidate_replay_guard_policy_20260528.csv",
        "stock_ai_candidate_replay_dimension_summary_latest.json",
        "stock_ai_candidate_replay_dimension_summary_20260528.json",
        "stock_ai_candidate_replay_dimension_summary_latest.csv",
        "stock_ai_candidate_replay_dimension_summary_20260528.csv",
        "stock_ai_candidate_replay_case_tag_summary_latest.json",
        "stock_ai_candidate_replay_case_tag_summary_20260528.json",
        "stock_ai_candidate_replay_case_tag_summary_latest.csv",
        "stock_ai_candidate_replay_case_tag_summary_20260528.csv",
        "stock_ai_candidate_replay_model_summary_latest.json",
        "stock_ai_candidate_replay_model_summary_20260528.json",
        "stock_ai_candidate_replay_model_summary_latest.csv",
        "stock_ai_candidate_replay_model_summary_20260528.csv",
        "stock_ai_candidate_replay_outcome_audit_latest.json",
        "stock_ai_candidate_replay_outcome_audit_20260528.json",
        "stock_ai_candidate_replay_outcome_audit_latest.csv",
        "stock_ai_candidate_replay_outcome_audit_20260528.csv",
    }
    latest_rows = [
        json.loads(line)
        for line in (tmp_path / "stock_ai_candidate_replay_outcome_latest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(latest_rows) == 10
    assert {row["outcome_status"] for row in latest_rows} == {"available", "pending_future_price"}
    summary = json.loads(
        (tmp_path / "stock_ai_candidate_replay_outcome_summary_latest.json").read_text(encoding="utf-8")
    )
    assert summary
    effects = json.loads(
        (tmp_path / "stock_ai_candidate_replay_invalidation_effect_latest.json").read_text(encoding="utf-8")
    )
    assert effects
    guard_policy = json.loads(
        (tmp_path / "stock_ai_candidate_replay_guard_policy_latest.json").read_text(encoding="utf-8")
    )
    assert guard_policy
    dimensions = json.loads(
        (tmp_path / "stock_ai_candidate_replay_dimension_summary_latest.json").read_text(encoding="utf-8")
    )
    assert dimensions
    case_tags = json.loads(
        (tmp_path / "stock_ai_candidate_replay_case_tag_summary_latest.json").read_text(encoding="utf-8")
    )
    assert case_tags
    model_summary = json.loads(
        (tmp_path / "stock_ai_candidate_replay_model_summary_latest.json").read_text(encoding="utf-8")
    )
    assert model_summary
    audit = json.loads(
        (tmp_path / "stock_ai_candidate_replay_outcome_audit_latest.json").read_text(encoding="utf-8")
    )
    assert audit["overall_status"] == "PASS"


def test_replay_outcome_cli_fails_on_audit_fail_unless_no_fail(tmp_path) -> None:
    queue_path = tmp_path / "stock_ai_candidate_replay_queue_latest.jsonl"
    price_path = tmp_path / "prices.csv"
    queue_rows = build_replay_queue_rows([_snapshot_row("688981", "a" * 64)])
    _write_jsonl(queue_path, queue_rows)
    _write_price_csv(price_path, [])

    args = [
        "--queue-path",
        str(queue_path),
        "--price-path",
        str(price_path),
        "--output-dir",
        str(tmp_path),
    ]

    assert replay_outcome_main(args) == 1
    assert replay_outcome_main([*args, "--no-fail"]) == 0
