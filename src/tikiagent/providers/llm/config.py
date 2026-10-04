"""模型后端配置。"""

from dataclasses import dataclass
from pathlib import Path
import os
import math

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
    context_limit: int = 32_000
    max_output_tokens: int = 2_000

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("模型请求超时必须是有限正数")
        if self.max_retries < 0 or self.max_retries > 3:
            raise ValueError("模型请求重试次数必须在 0～3 之间")
        if not 0 < self.max_output_tokens < self.context_limit:
            raise ValueError("输出预算必须大于零且小于模型窗口")

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "ModelSettings":
        load_dotenv(dotenv_path=env_file)
        return cls(
            api_key=_required_env("TIKI_LLM_API_KEY"),
            base_url=_required_env("TIKI_LLM_BASE_URL"),
            model=_required_env("TIKI_LLM_MODEL"),
            timeout_seconds=float(os.getenv("TIKI_LLM_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("TIKI_LLM_MAX_RETRIES", "1")),
            context_limit=int(os.getenv("TIKI_LLM_CONTEXT_LIMIT", "32000")),
            max_output_tokens=int(os.getenv("TIKI_LLM_MAX_OUTPUT_TOKENS", "2000")),
        )
