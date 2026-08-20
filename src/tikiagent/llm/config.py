"""模型后端配置。"""

import os
from dataclasses import dataclass
from pathlib import Path

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

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "ModelSettings":
        load_dotenv(dotenv_path=env_file)
        return cls(
            api_key=_required_env("TIKI_LLM_API_KEY"),
            base_url=_required_env("TIKI_LLM_BASE_URL"),
            model=_required_env("TIKI_LLM_MODEL"),
        )
