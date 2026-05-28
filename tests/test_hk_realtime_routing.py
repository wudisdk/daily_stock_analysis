# -*- coding: utf-8 -*-
"""
Regression tests for Hong Kong realtime quote routing.
"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

if "litellm" not in sys.modules:
    sys.modules["litellm"] = MagicMock()
if "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import UnifiedRealtimeQuote


class _DummyFetcher:
    def __init__(self, name: str, priority: int, result=None):
        self.name = name
        self.priority = priority
        self.result = result
        self.calls = []

    def get_realtime_quote(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class TestHKRealtimeRouting(unittest.TestCase):
    """Ensure HK realtime lookup does not fan out into A-share sources."""

    @patch("src.config.get_config")
    def test_manager_routes_hk_suffix_to_yfinance_before_slow_akshare(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="tencent,akshare_sina,efinance,akshare_em,tushare",
        )

        result = UnifiedRealtimeQuote(code="HK01810", price=38.2)
        yfinance = _DummyFetcher("YfinanceFetcher", 0, result=result)
        akshare = _DummyFetcher("AkshareFetcher", 1, result=None)
        efinance = _DummyFetcher("EfinanceFetcher", 2, result={"should": "not be called"})
        tushare = _DummyFetcher("TushareFetcher", 3, result={"should": "not be called"})

        manager = DataFetcherManager(fetchers=[efinance, yfinance, akshare, tushare])
        quote = manager.get_realtime_quote("1810.HK")

        self.assertIs(quote, result)
        self.assertEqual(yfinance.calls, [(("HK01810",), {})])
        self.assertEqual(akshare.calls, [])
        self.assertEqual(efinance.calls, [])
        self.assertEqual(tushare.calls, [])

    @patch("src.config.get_config")
    def test_manager_falls_back_to_akshare_for_hk_when_yfinance_empty(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="tencent,akshare_sina,efinance,akshare_em,tushare",
        )

        efinance = _DummyFetcher("EfinanceFetcher", 0, result={"should": "not be called"})
        yfinance = _DummyFetcher("YfinanceFetcher", 1, result=None)
        result = UnifiedRealtimeQuote(code="HK01810", price=38.2)
        akshare = _DummyFetcher("AkshareFetcher", 2, result=result)
        tushare = _DummyFetcher("TushareFetcher", 3, result={"should": "not be called"})

        manager = DataFetcherManager(fetchers=[efinance, yfinance, akshare, tushare])
        quote = manager.get_realtime_quote("1810.HK")

        self.assertIs(quote, result)
        self.assertEqual(yfinance.calls, [(("HK01810",), {})])
        self.assertEqual(akshare.calls, [(("HK01810",), {"source": "hk"})])
        self.assertEqual(efinance.calls, [])
        self.assertEqual(tushare.calls, [])


if __name__ == "__main__":
    unittest.main()
