# -*- coding: utf-8 -*-
"""Tests for low-sensitivity AI candidate price-history export."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.services.ai_snapshot_price_history import (
    audit_price_history,
    build_price_history_rows,
    load_candidate_specs,
    write_price_history_audit_outputs,
    write_price_history_outputs,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as con:
        con.execute(
            """
            CREATE TABLE stock_daily (
                code TEXT NOT NULL,
                date TEXT NOT NULL,
                close REAL,
                data_source TEXT
            )
            """
        )
        con.executemany(
            "INSERT INTO stock_daily (code, date, close, data_source) VALUES (?, ?, ?, ?)",
            [
                ("600519", "2026-05-28", 100.0, "unit"),
                ("600519", "2026-05-29", 101.0, "unit"),
                ("600519", "2026-06-01", 103.0, "unit"),
                ("000001.SZ", "2026-05-28", 10.0, "unit"),
            ],
        )


def test_load_candidate_specs_merges_snapshot_and_queue_horizons(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snapshot.jsonl"
    queue_path = tmp_path / "queue.jsonl"
    _write_jsonl(
        snapshot_path,
        [{"stock_code": "600519.SH", "stock_name": "Kweichow", "trade_date": "2026-05-28"}],
    )
    _write_jsonl(
        queue_path,
        [
            {
                "stock_code": "600519.SH",
                "stock_name": "Kweichow",
                "trade_date": "2026-05-28",
                "forward_trading_days": 3,
            }
        ],
    )

    candidates = load_candidate_specs(snapshot_path=snapshot_path, queue_path=queue_path)

    assert candidates == [
        {
            "stock_code": "600519.SH",
            "stock_name": "Kweichow",
            "anchor_dates": [{"trade_date": "2026-05-28", "max_forward_trading_days": 3}],
        }
    ]


def test_build_price_history_rows_maps_db_alias_to_candidate_code(tmp_path: Path) -> None:
    db_path = tmp_path / "stock.db"
    _make_db(db_path)
    candidates = [
        {
            "stock_code": "600519.SH",
            "stock_name": "Kweichow",
            "anchor_dates": [{"trade_date": "2026-05-28", "max_forward_trading_days": 1}],
        }
    ]

    rows = build_price_history_rows(
        candidates,
        database_path=db_path,
        generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
    )

    assert [row["trade_date"] for row in rows] == ["2026-05-28", "2026-05-29", "2026-06-01"]
    assert {row["stock_code"] for row in rows} == {"600519.SH"}
    assert rows[0]["db_code"] == "600519"
    assert rows[0]["close"] == 100.0


def test_build_price_history_rows_database_scope_includes_non_candidate_codes(tmp_path: Path) -> None:
    db_path = tmp_path / "stock.db"
    _make_db(db_path)
    candidates = [
        {
            "stock_code": "600519.SH",
            "stock_name": "Kweichow",
            "anchor_dates": [{"trade_date": "2026-05-28", "max_forward_trading_days": 1}],
        }
    ]

    rows = build_price_history_rows(candidates, database_path=db_path, scope="database")

    assert ("600519.SH", "2026-05-28") in {(row["stock_code"], row["trade_date"]) for row in rows}
    assert ("000001.SZ", "2026-05-28") in {(row["stock_code"], row["trade_date"]) for row in rows}


def test_write_price_history_outputs_and_audit_files(tmp_path: Path) -> None:
    db_path = tmp_path / "stock.db"
    snapshot_path = tmp_path / "snapshot.jsonl"
    queue_path = tmp_path / "queue.jsonl"
    output_dir = tmp_path / "out"
    _make_db(db_path)
    _write_jsonl(
        snapshot_path,
        [{"stock_code": "600519.SH", "stock_name": "Kweichow", "trade_date": "2026-05-28"}],
    )
    _write_jsonl(
        queue_path,
        [{"stock_code": "600519.SH", "trade_date": "2026-05-28", "forward_trading_days": 1}],
    )

    paths = write_price_history_outputs(
        snapshot_path=snapshot_path,
        queue_path=queue_path,
        database_path=db_path,
        output_dir=output_dir,
        generated_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
    )
    names = {path.name for path in paths}

    assert "stock_ai_candidate_price_history_latest.jsonl" in names
    assert "stock_ai_candidate_price_history_20260601.jsonl" in names
    with (output_dir / "stock_ai_candidate_price_history_latest.csv").open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        csv_rows = list(csv.DictReader(file))
    assert csv_rows[0]["stock_code"] == "600519.SH"

    candidates = load_candidate_specs(snapshot_path=snapshot_path, queue_path=queue_path)
    rows = build_price_history_rows(candidates, database_path=db_path)
    audit = audit_price_history(candidates, rows, database_path=db_path)
    audit_paths = write_price_history_audit_outputs(audit, output_dir)

    assert audit["overall_status"] == "PASS"
    assert {path.name for path in audit_paths} >= {
        "stock_ai_candidate_price_history_audit_latest.json",
        "stock_ai_candidate_price_history_audit_20260601.json",
        "stock_ai_candidate_price_history_audit_latest.csv",
    }


def test_audit_price_history_warns_for_incomplete_forward_window(tmp_path: Path) -> None:
    db_path = tmp_path / "stock.db"
    _make_db(db_path)
    candidates = [
        {
            "stock_code": "600519.SH",
            "stock_name": "Kweichow",
            "anchor_dates": [{"trade_date": "2026-05-28", "max_forward_trading_days": 5}],
        }
    ]
    rows = build_price_history_rows(candidates, database_path=db_path)

    audit = audit_price_history(candidates, rows, database_path=db_path)
    checks = {check["check"]: check for check in audit["checks"]}

    assert audit["overall_status"] == "WARN"
    assert checks["price_history_forward_window_coverage"]["status"] == "WARN"


def test_audit_price_history_fails_missing_candidate_rows(tmp_path: Path) -> None:
    candidates = [{"stock_code": "688981", "stock_name": "SMIC", "anchor_dates": []}]

    audit = audit_price_history(candidates, [], database_path=tmp_path / "missing.db")
    checks = {check["check"]: check for check in audit["checks"]}

    assert audit["overall_status"] == "FAIL"
    assert checks["price_history_rows_non_empty"]["status"] == "FAIL"
    assert checks["price_history_candidate_coverage"]["status"] == "FAIL"
