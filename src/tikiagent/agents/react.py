"""最小但完整的 ReAct Agent Loop。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tikiagent.harness.dispatcher import Dispatcher
from tikiagent.harness.models import ToolError, ToolResult
from tikiagent.llm.models import ModelClient


DEFAULT_SYSTEM_PROMPT = """你是 TikiAgent 的文件任务 Agent。
只能通过提供的工具观察和修改 Workspace，不要假设文件内容。
工具失败时分析结构化错误并调整下一步。
修改文件后必须获取足够证据，再给出简洁的最终回答。
"""


class MaxStepsExceeded(RuntimeError):
    """Agent 在限制内未产生最终回答。"""


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    final_text: str
    steps: int
    tool_results: tuple[ToolResult, ...]
    messages: tuple[dict[str, Any], ...]


class ReActAgent:
    """连接模型决策与 Execution Harness 的循环。"""

    def __init__(
        self,
        model: ModelClient,
        dispatcher: Dispatcher,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 8,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须大于 0")
        self.model = model
        self.dispatcher = dispatcher
        self.system_prompt = system_prompt
        self.max_steps = max_steps

    def run(self, task: str) -> AgentRunResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        tool_results: list[ToolResult] = []

        for step in range(1, self.max_steps + 1):
            response = self.model.complete(
                messages=messages,
                tool_schemas=self.dispatcher.registry.schemas(),
            )
            messages.append(response.assistant_message)

            if response.tool_calls:
                for model_tool_call in response.tool_calls:
                    result = self._execute_tool_call(
                        tool_call_id=model_tool_call.tool_call_id,
                        name=model_tool_call.name,
                        arguments_json=model_tool_call.arguments_json,
                    )
                    tool_results.append(result)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.tool_call_id,
                            "content": result.model_dump_json(),
                        }
                    )
                continue

            if response.final_text is not None:
                return AgentRunResult(
                    final_text=response.final_text,
                    steps=step,
                    tool_results=tuple(tool_results),
                    messages=tuple(messages),
                )

            raise RuntimeError("模型既没有返回 ToolCall，也没有最终文本")

        raise MaxStepsExceeded(f"Agent 超过最大步数：{self.max_steps}")

    def _execute_tool_call(
        self,
        tool_call_id: str,
        name: str,
        arguments_json: str,
    ) -> ToolResult:
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as error:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=name,
                ok=False,
                error=ToolError(
                    code="invalid_arguments_json",
                    message="模型生成的工具参数不是合法 JSON",
                    details={
                        "arguments": arguments_json,
                        "error": str(error),
                    },
                ),
            )

        raw_tool_call: Mapping[str, Any] = {
            "tool_call_id": tool_call_id,
            "name": name,
            "arguments": arguments,
        }
        return self.dispatcher.dispatch(raw_tool_call)
