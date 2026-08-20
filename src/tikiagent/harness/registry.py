"""工具注册表。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """工具描述、参数模型与 Python 实现的绑定。"""

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[..., Any]


class ToolRegistry:
    """保存 Agent 可用工具，并生成厂商无关的 Tool Schema。"""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """返回内部 Schema，由 ModelClient 转换成供应商协议。"""

        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.args_model.model_json_schema(),
            }
            for tool in self._tools.values()
        ]
