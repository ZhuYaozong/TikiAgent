"""Tavily Provider 与 Web Registry 的离线测试。"""

from typing import Any

import httpx
import pytest

from tikiagent.providers.search.config import SearchSettings
from tikiagent.providers.search.tavily import TavilyProvider
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.web import build_web_registry


class FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.data


class FakeClient:
    def __init__(self, data: Any) -> None:
        self.data = data
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.data)


def test_search_normalizes_and_limits_results() -> None:
    client = FakeClient(
        {
            "results": [
                {
                    "title": "Release",
                    "url": "https://example.com/release",
                    "content": "x" * 3_000,
                    "score": 0.9,
                }
            ],
            "request_id": "request-1",
        }
    )
    provider = TavilyProvider(
        SearchSettings(api_key="secret"),
        client=client,
    )

    result = provider.search("agent framework", max_results=3)

    assert len(result["results"][0]["snippet"]) == 2_000
    assert client.calls[0]["url"] == "https://api.tavily.com/search"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert client.calls[0]["json"]["include_raw_content"] is False


def test_registry_exposes_only_web_tools() -> None:
    provider = TavilyProvider(
        SearchSettings(api_key="offline"),
        client=FakeClient({"results": []}),
    )
    names = {item["name"] for item in build_web_registry(provider).schemas()}

    assert names == {"web_search", "web_extract"}


def test_extract_rejects_private_url_before_http_call() -> None:
    client = FakeClient({"results": []})
    dispatcher = Dispatcher(
        build_web_registry(
            TavilyProvider(
                SearchSettings(api_key="offline"),
                client=client,
            )
        )
    )

    result = dispatcher.dispatch(
        {
            "tool_call_id": "extract-1",
            "name": "web_extract",
            "arguments": {"url": "http://127.0.0.1/private"},
        }
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "private_url_denied"
    assert client.calls == []


class TimeoutClient:
    def post(self, url: str, **kwargs: Any) -> None:
        del kwargs
        raise httpx.ReadTimeout("timeout", request=httpx.Request("POST", url))


def test_timeout_becomes_structured_tool_error() -> None:
    dispatcher = Dispatcher(
        build_web_registry(
            TavilyProvider(
                SearchSettings(api_key="offline"),
                client=TimeoutClient(),
            )
        )
    )

    result = dispatcher.dispatch(
        {
            "tool_call_id": "search-1",
            "name": "web_search",
            "arguments": {"query": "agent"},
        }
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "web_timeout"


def test_search_settings_requires_tavily_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TIKI_SEARCH_PROVIDER", "tavily")
    monkeypatch.delenv("TIKI_TAVILY_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="TIKI_TAVILY_API_KEY"):
        SearchSettings.from_env(env_file="missing.env")
