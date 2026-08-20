"""模型供应商无关的响应模型与接口。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    tool_call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """供应商响应经过适配后的统一格式。"""

    assistant_message: dict[str, Any]
    tool_calls: tuple[ModelToolCall, ...] = ()
    final_text: str | None = None


class ModelClient(Protocol):
    """ReAct Agent 依赖的最小模型接口。"""

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_schemas: Sequence[Mapping[str, Any]],
    ) -> ModelResponse: ...
