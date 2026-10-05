"""模型后端配置。"""

from dataclasses import dataclass
from pathlib import Path
import os
import math
from urllib.parse import urlparse

from dotenv import load_dotenv


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少模型配置：{name}")
    return value


@dataclass(frozen=True, slots=True)
class ModelSettings:
    """OpenAI-compatible 后端所需的最小配置。"""

    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 60.0
    max_retries: int = 1
    context_limit: int = 131_072
    max_output_tokens: int = 2_000
    safety_margin: int = 0
    api_style: str = "openai"
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("模型请求超时必须是有限正数")
        if self.max_retries < 0 or self.max_retries > 3:
            raise ValueError("模型请求重试次数必须在 0～3 之间")
        if self.max_output_tokens <= 0 or not 0 < self.max_output_tokens + self.safety_margin < self.context_limit or self.safety_margin < 0:
            raise ValueError("输出预算必须大于零且小于模型窗口")
        if self.api_style not in {"openai", "deepseek"}:
            raise ValueError("API 适配必须为 openai/deepseek")
        if self.reasoning_effort not in {None, "none", "low", "high", "max"}:
            raise ValueError("思考强度必须为 none/low/high/max")

    def generation_options(self) -> dict:
        """仅明确适配 DeepSeek 时发送专用参数；未知中转站保持通用协议。"""
        if self.api_style != "deepseek" or self.reasoning_effort is None:
            return {}
        if self.reasoning_effort == "none":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"reasoning_effort": self.reasoning_effort,
                "extra_body": {"thinking": {"type": "enabled"}}}

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "ModelSettings":
        load_dotenv(dotenv_path=env_file)
        base_url = _required_env("TIKI_LLM_BASE_URL")
        api_style = os.getenv("TIKI_LLM_API_STYLE", "auto")
        if api_style == "auto":
            api_style = "deepseek" if urlparse(base_url).hostname == "api.deepseek.com" else "openai"
        return cls(
            api_key=_required_env("TIKI_LLM_API_KEY"),
            base_url=base_url,
            model=_required_env("TIKI_LLM_MODEL"),
            timeout_seconds=float(os.getenv("TIKI_LLM_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("TIKI_LLM_MAX_RETRIES", "1")),
            context_limit=int(os.getenv("TIKI_LLM_CONTEXT_LIMIT", "131072")),
            max_output_tokens=int(os.getenv("TIKI_LLM_MAX_OUTPUT_TOKENS", "2000")),
            api_style=api_style,
        )
