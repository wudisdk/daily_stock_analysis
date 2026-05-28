# -*- coding: utf-8 -*-
"""Regression tests for post-merge Tushare follow-up fixes."""

import importlib.util
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.tushare_fetcher import TushareFetcher


class TestTushareFetcherFollowUps(unittest.TestCase):
    """Cover rate limiting and cross-day trade-calendar refresh behavior."""

    @staticmethod
    def _make_fetcher() -> TushareFetcher:
        with patch.object(TushareFetcher, "_init_api", return_value=None):
            fetcher = TushareFetcher()
        fetcher._api = MagicMock()
        fetcher.priority = 2
        return fetcher

    def test_get_trade_time_refreshes_trade_calendar_when_day_changes(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.side_effect = [
            pd.DataFrame({"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}),
            pd.DataFrame({"cal_date": ["20260318", "20260317"], "is_open": [1, 1]}),
        ]

        with patch.object(
            fetcher,
            "_get_china_now",
            side_effect=[
                datetime(2026, 3, 17, 20, 0),
                datetime(2026, 3, 17, 20, 0),
                datetime(2026, 3, 18, 20, 0),
                datetime(2026, 3, 18, 20, 0),
            ],
        ), patch.object(fetcher, "_check_rate_limit") as rate_limit_mock:
            self.assertEqual(fetcher.get_trade_time(early_time="00:00", late_time="19:00"), "20260317")
            self.assertEqual(fetcher.get_trade_time(early_time="00:00", late_time="19:00"), "20260318")

        self.assertEqual(fetcher._api.trade_cal.call_count, 2)
        self.assertEqual(rate_limit_mock.call_count, 2)
    def test_get_trade_time_returns_latest_trade_date_on_non_trade_day(self) -> None:
        """Non-trade day (e.g. Saturday) should return the most recent trade
        date (Friday), not the one before it (Thursday).  Fixes #1009."""
        fetcher = self._make_fetcher()
        # 2026-03-21 is Saturday; Friday 20 and Thursday 19 are trade dates
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {
                "cal_date": ["20260314", "20260315", "20260316",
                             "20260317", "20260318", "20260319",
                             "20260320", "20260321"],
                "is_open": [0, 0, 1, 1, 1, 1, 1, 0],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            # called twice: once by get_trade_time, once by _get_trade_dates
            side_effect=[datetime(2026, 3, 21, 10, 0)] * 2,
        ), patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_trade_time(early_time="00:00", late_time="19:00")

        # Should be Friday (20th), NOT Thursday (19th)
        self.assertEqual(result, "20260320")

    def test_get_trade_time_trade_day_before_data_ready_returns_previous(self) -> None:
        """On a trade day within the early-late window, should return the
        previous trade date (data not ready yet for today)."""
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {
                "cal_date": ["20260319", "20260320"],
                "is_open": [1, 1],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            # Friday 10:00 AM - within 00:00~19:00 window, data not ready
            side_effect=[datetime(2026, 3, 20, 10, 0)] * 2,
        ), patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_trade_time(early_time="00:00", late_time="19:00")

        # Data not ready, should fall back to Thursday (19th)
        self.assertEqual(result, "20260319")
        
          
    def test_get_sector_rankings_rate_limits_calendar_and_rankings_api(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}
        )
        fetcher._api.moneyflow_ind_ths.return_value = pd.DataFrame(
            {
                "industry": ["AI", "消费"],
                "pct_change": [1.8, -0.6],
            }
        )

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 3, 17, 16, 0)), patch.object(
            fetcher, "_check_rate_limit"
        ) as rate_limit_mock:
            top, bottom = fetcher.get_sector_rankings(n=1)

        self.assertEqual(top, [{"name": "AI", "change_pct": 1.8}])
        self.assertEqual(bottom, [{"name": "消费", "change_pct": -0.6}])
        self.assertEqual(rate_limit_mock.call_count, 2)

    def test_get_chip_distribution_rate_limits_all_tushare_calls(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}
        )
        fetcher._api.cyq_chips.return_value = pd.DataFrame(
            {
                "price": [9.0, 10.0, 11.0],
                "percent": [20.0, 50.0, 30.0],
            }
        )
        fetcher._api.daily.return_value = pd.DataFrame({"close": [10.5]})

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 3, 17, 20, 0)), patch.object(
            fetcher, "_check_rate_limit"
        ) as rate_limit_mock:
            chip = fetcher.get_chip_distribution("600519")

        self.assertIsNotNone(chip)
        if chip is None:
            self.fail("expected chip distribution data")
        self.assertEqual(chip.date, "2026-03-17")
        self.assertAlmostEqual(chip.profit_ratio, 0.7)
        self.assertAlmostEqual(chip.avg_cost, 10.1)
        self.assertAlmostEqual(chip.concentration_90, 0.1)
        self.assertAlmostEqual(chip.concentration_70, 0.1)
        self.assertEqual(rate_limit_mock.call_count, 3)

    def test_convert_stock_code_accepts_exchange_prefixed_a_share(self) -> None:
        fetcher = self._make_fetcher()

        self.assertEqual(fetcher._convert_stock_code("SZ000001"), "000001.SZ")
        self.assertEqual(fetcher._convert_stock_code("SH600519"), "600519.SH")
        self.assertEqual(fetcher._convert_stock_code("605218"), "605218.SH")
        self.assertEqual(fetcher._convert_stock_code("600519.SS"), "600519.SH")

    def test_get_daily_basic_snapshot_returns_latest_valuation_fields(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260318", "20260317"], "is_open": [1, 1]}
        )
        fetcher._api.daily_basic.return_value = pd.DataFrame(
            {
                "ts_code": ["600519.SH"],
                "trade_date": ["20260318"],
                "turnover_rate": [0.82],
                "volume_ratio": [1.25],
                "pe_ttm": [25.6],
                "pb": [7.8],
                "total_mv": [1800000.0],
                "circ_mv": [1500000.0],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            side_effect=[datetime(2026, 3, 18, 18, 0)] * 4,
        ), patch.object(fetcher, "_check_rate_limit"):
            snapshot = fetcher.get_daily_basic_snapshot("600519")

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["ts_code"], "600519.SH")
        self.assertEqual(snapshot["trade_date"], "20260318")
        self.assertAlmostEqual(snapshot["turnover_rate"], 0.82)
        self.assertAlmostEqual(snapshot["volume_ratio"], 1.25)
        self.assertAlmostEqual(snapshot["pe_ttm"], 25.6)
        self.assertAlmostEqual(snapshot["pb"], 7.8)
        self.assertAlmostEqual(snapshot["total_mv"], 1800000.0 * 10000)
        self.assertEqual(snapshot["unit_normalized"]["total_mv"], "yuan")
        fetcher._api.daily_basic.assert_called_once()

    def test_realtime_quote_supplements_missing_fields_from_daily_basic(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.quotation.return_value = pd.DataFrame(
            {
                "name": ["贵州茅台"],
                "price": [1600.0],
                "pct_chg": [1.2],
                "change": [19.0],
                "vol": [10000],
                "amount": [16000000.0],
                "high": [1610.0],
                "low": [1580.0],
                "open": [1590.0],
                "pre_close": [1581.0],
            }
        )
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260318", "20260317"], "is_open": [1, 1]}
        )
        fetcher._api.daily_basic.return_value = pd.DataFrame(
            {
                "ts_code": ["600519.SH"],
                "trade_date": ["20260318"],
                "turnover_rate": [0.82],
                "volume_ratio": [1.25],
                "pe_ttm": [25.6],
                "pb": [7.8],
                "total_mv": [1800000.0],
                "circ_mv": [1500000.0],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            side_effect=[datetime(2026, 3, 18, 18, 0)] * 4,
        ), patch.object(fetcher, "_check_rate_limit"):
            quote = fetcher.get_realtime_quote("600519")

        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertAlmostEqual(quote.volume_ratio, 1.25)
        self.assertAlmostEqual(quote.turnover_rate, 0.82)
        self.assertAlmostEqual(quote.pe_ratio, 25.6)
        self.assertAlmostEqual(quote.pb_ratio, 7.8)
        self.assertAlmostEqual(quote.total_mv, 1800000.0 * 10000)
        self.assertAlmostEqual(quote.circ_mv, 1500000.0 * 10000)

    def test_get_fina_indicator_snapshot_applies_point_in_time_ann_date_filter(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.fina_indicator.return_value = pd.DataFrame(
            {
                "ts_code": ["600519.SH", "600519.SH", "600519.SH"],
                "ann_date": ["20260430", "20260329", "20251030"],
                "end_date": ["20260331", "20251231", "20250930"],
                "roe": [7.0, 28.5, 21.1],
                "grossprofit_margin": [90.0, 91.5, 90.5],
                "netprofit_margin": [50.0, 52.1, 51.0],
            }
        )

        with patch.object(fetcher, "_check_rate_limit"):
            snapshot = fetcher.get_fina_indicator_snapshot(
                "600519",
                asof_date="20260330",
                start_date="20250101",
                limit=2,
            )

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["asof_date"], "20260330")
        self.assertEqual(snapshot["point_in_time_filter"], "ann_date <= asof_date")
        self.assertEqual(snapshot["latest"]["ann_date"], "20260329")
        self.assertEqual(len(snapshot["recent"]), 2)
        self.assertTrue(all(int(row["ann_date"]) <= 20260330 for row in snapshot["recent"]))

    @patch.dict(sys.modules, {"tushare": MagicMock()})
    def test_legacy_realtime_quote_keeps_sz_hint_as_stock_symbol(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.quotation.side_effect = Exception("quota")

        tushare_module = sys.modules["tushare"]
        tushare_module.get_realtime_quotes.return_value = pd.DataFrame(
            [
                {
                    "name": "平安银行",
                    "price": "10.94",
                    "pre_close": "10.88",
                    "volume": "1000",
                    "amount": "2000",
                    "high": "11.00",
                    "low": "10.80",
                    "open": "10.90",
                }
            ]
        )

        with patch.object(fetcher, "_supplement_quote_from_daily_basic"):
            quote = fetcher.get_realtime_quote("SZ000001")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.code, "000001")
        self.assertEqual(quote.name, "平安银行")
        tushare_module.get_realtime_quotes.assert_called_once_with("000001")
