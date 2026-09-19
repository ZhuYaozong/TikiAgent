"""Tavily HTTP 适配与外部 URL 检查。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse
import ipaddress

import httpx

from tikiagent.providers.search.config import SearchSettings
from tikiagent.tools.models import ToolExecutionError


class TavilyProvider:
    """使用 Tavily HTTP API 的可替换 Web Provider。"""

    def __init__(
        self,
        settings: SearchSettings,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or httpx.Client(
            timeout=settings.timeout_seconds,
            follow_redirects=True,
        )

    def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> dict[str, Any]:
        """执行 basic search，返回长度受限的结构化来源。"""

        data = self._post(
            "/search",
            {
                "query": query,
                "topic": "general",
                "search_depth": "basic",
                "max_results": max_results,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
            },
        )
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise ToolExecutionError(
                "invalid_search_response",
                "Tavily Search 响应缺少 results",
            )

        results: list[dict[str, Any]] = []
        for item in raw_results[:max_results]:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            url = item.get("url")
            if not isinstance(title, str) or not isinstance(url, str):
                continue
            content = item.get("content")
            score = item.get("score")
            results.append(
                {
                    "title": title,
                    "url": url,
                    "snippet": (
                        content[:2_000]
                        if isinstance(content, str)
                        else ""
                    ),
                    "score": (
                        float(score)
                        if isinstance(score, int | float)
                        else None
                    ),
                    "published_date": item.get("published_date"),
                }
            )
        return {
            "query": query,
            "results": results,
            "request_id": data.get("request_id"),
            "response_time": data.get("response_time"),
        }

    def extract(
        self,
        url: str,
        content_limit: int = 6_000,
    ) -> dict[str, Any]:
        """读取单个公共网页正文并限制返回给模型的长度。"""

        validate_public_url(url)
        data = self._post(
            "/extract",
            {
                "urls": [url],
                "extract_depth": "basic",
                "include_images": False,
                "format": "markdown",
            },
        )
        raw_results = data.get("results")
        if not isinstance(raw_results, list) or not raw_results:
            raise ToolExecutionError(
                "extract_failed",
                "Tavily 未返回网页内容",
                {"url": url, "failed_results": data.get("failed_results", [])},
            )
        first = raw_results[0]
        if not isinstance(first, dict) or not isinstance(
            first.get("raw_content"), str
        ):
            raise ToolExecutionError(
                "invalid_extract_response",
                "Tavily Extract 响应缺少 raw_content",
            )
        content = first["raw_content"]
        return {
            "url": str(first.get("url", url)),
            "content": content[:content_limit],
            "truncated": len(content) > content_limit,
            "original_characters": len(content),
            "request_id": data.get("request_id"),
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.post(
                f"{self.settings.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
        except httpx.TimeoutException as error:
            raise ToolExecutionError(
                "web_timeout",
                "Tavily 请求超时",
                {"endpoint": path},
            ) from error
        except httpx.HTTPStatusError as error:
            raise ToolExecutionError(
                "web_http_error",
                "Tavily 返回非成功状态码",
                {
                    "endpoint": path,
                    "status_code": error.response.status_code,
                },
            ) from error
        except httpx.RequestError as error:
            raise ToolExecutionError(
                "web_request_error",
                "无法连接 Tavily",
                {
                    "endpoint": path,
                    "exception_type": type(error).__name__,
                },
            ) from error

        try:
            data = response.json()
        except ValueError as error:
            raise ToolExecutionError(
                "invalid_web_response",
                "Tavily 返回的内容不是合法 JSON",
                {"endpoint": path},
            ) from error
        if not isinstance(data, dict):
            raise ToolExecutionError(
                "invalid_web_response",
                "Tavily JSON 顶层必须是对象",
                {"endpoint": path},
            )
        return data


def validate_public_url(url: str) -> None:
    """拒绝明显的本地、私有和非 HTTP(S) URL。"""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolExecutionError(
            "invalid_url",
            "只允许包含主机名的 HTTP(S) URL",
            {"url": url},
        )
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ToolExecutionError(
            "private_url_denied",
            "拒绝提取本地地址",
            {"url": url},
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ToolExecutionError(
            "private_url_denied",
            "拒绝提取非公网 IP",
            {"url": url},
        )
