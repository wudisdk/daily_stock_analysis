# -*- coding: utf-8 -*-
"""Tests for no-key news RSS search fallback."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from src.search_service import (
    NewsRSSSearchProvider,
    SearchResponse,
    SearchResult,
    SearchService,
)


class _FakeHTTPResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _StaticProvider:
    def __init__(self, name: str, response: SearchResponse) -> None:
        self.name = name
        self._response = response

    @property
    def is_available(self) -> bool:
        return True

    def search(self, query: str, max_results: int = 5, days: int = 7, **_kwargs) -> SearchResponse:
        return SearchResponse(
            query=query,
            results=self._response.results[:max_results],
            provider=self.name,
            success=self._response.success,
            error_message=self._response.error_message,
        )


def _rss_item(title: str, pub_date: str) -> str:
    return f"""
    <item>
      <title>{title}</title>
      <link>https://example.com/{title}</link>
      <description><![CDATA[<p>{title} market update</p>]]></description>
      <pubDate>{pub_date}</pubDate>
      <source url="https://example.com">Example News</source>
    </item>
    """


def test_news_rss_provider_parses_recent_rss_items() -> None:
    pub_date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    feed = f"<rss><channel>{_rss_item('SMIC raises guidance', pub_date)}</channel></rss>"

    provider = NewsRSSSearchProvider()
    with patch("src.search_service._get_with_retry", return_value=_FakeHTTPResponse(feed)) as mock_get:
        response = provider.search("SMIC stock latest news", max_results=2, days=3)

    assert response.success is True
    assert response.provider == "NewsRSS"
    assert len(response.results) == 1
    assert response.results[0].title == "SMIC raises guidance"
    assert response.results[0].source == "Example News"
    assert mock_get.call_args_list[0].kwargs["params"]["hl"] == "zh-CN"


def test_search_service_adds_news_rss_before_public_searxng() -> None:
    service = SearchService(searxng_public_instances_enabled=True)

    provider_names = [provider.name for provider in service._providers]
    assert "NewsRSS" in provider_names
    assert "SearXNG" in provider_names
    assert provider_names.index("NewsRSS") < provider_names.index("SearXNG")


def test_comprehensive_intel_falls_back_to_news_rss_after_failed_provider() -> None:
    service = SearchService(searxng_public_instances_enabled=False)
    service._providers = [
        _StaticProvider(
            "Tavily",
            SearchResponse(
                query="x",
                results=[],
                provider="Tavily",
                success=False,
                error_message="API quota exceeded",
            ),
        ),
        _StaticProvider(
            "NewsRSS",
            SearchResponse(
                query="x",
                results=[
                    SearchResult(
                        title="SMIC quarterly result",
                        snippet="SMIC reports demand recovery",
                        url="https://example.com/smic",
                        source="Example News",
                        published_date=datetime.now(timezone.utc).date().isoformat(),
                    )
                ],
                provider="NewsRSS",
                success=True,
            ),
        ),
    ]

    intel = service.search_comprehensive_intel("HK00981", "SMIC", max_searches=1)

    assert intel["latest_news"].provider == "NewsRSS"
    assert intel["latest_news"].results
