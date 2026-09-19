"""搜索服务配置与环境变量加载。"""

from __future__ import annotations

from pathlib import Path
import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class SearchSettings(BaseModel):
    """搜索后端配置；真实 API Key 只从环境变量加载。"""

    api_key: str = Field(min_length=1)
    base_url: str = "https://api.tavily.com"
    timeout_seconds: float = Field(default=30.0, gt=0, le=60)

    @classmethod
    def from_env(
        cls,
        env_file: str | Path = ".env",
    ) -> "SearchSettings":
        load_dotenv(dotenv_path=env_file)
        provider = os.getenv("TIKI_SEARCH_PROVIDER", "tavily")
        if provider.lower() != "tavily":
            raise RuntimeError(
                "当前版本只支持 TIKI_SEARCH_PROVIDER=tavily"
            )
        api_key = os.getenv("TIKI_TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError("缺少搜索配置：TIKI_TAVILY_API_KEY")
        return cls(
            api_key=api_key,
            base_url=os.getenv(
                "TIKI_TAVILY_BASE_URL",
                "https://api.tavily.com",
            ).rstrip("/"),
        )
