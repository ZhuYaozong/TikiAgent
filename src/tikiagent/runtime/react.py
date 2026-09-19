"""通用 ReAct 循环：模型决策、工具观察与步数预算。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import json

from tikiagent.context.compression.models import ContextUsage
from tikiagent.context.memory.local import LocalMemoryManager
from tikiagent.context.models import BaseContext, ToolView, WorkingMemory
from tikiagent.context.preparation import ContextRuntime
from tikiagent.context.profiles import DEFAULT_CONTEXT_PROFILES
from tikiagent.harness.exposure import ToolExposureGuard
from tikiagent.providers.llm.models import ModelClient
from tikiagent.runtime.models import AgentRunResult, MaxStepsExceeded
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.models import ToolError, ToolResult


DEFAULT_SYSTEM_PROMPT = """你是 TikiAgent 的文件任务 Agent。
只能通过提供的工具观察和修改 Workspace，不要假设文件内容。
工具失败时分析结构化错误并调整下一步。
修改文件后必须获取足够证据，再给出简洁的最终回答。
"""


class ReActAgent:
    """连接模型决策与 Execution Harness 的循环。"""

    def __init__(
        self,
        model: ModelClient,
        dispatcher: Dispatcher,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 8,
        context_runtime: ContextRuntime | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须大于 0")
        self.model = model
        self.dispatcher = dispatcher
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        if context_runtime is None:
            code_profile = DEFAULT_CONTEXT_PROFILES["code_agent"]
            code_profile = code_profile.model_copy(
                update={
                    "system_rules": [
                        system_prompt.strip(),
                        *code_profile.system_rules,
                    ]
                }
            )
            context_runtime = ContextRuntime(
                profiles={"code_agent": code_profile}
            )
        self.context_runtime = context_runtime

    def run(
        self,
        task: str,
        *,
        base_context: BaseContext | None = None,
    ) -> AgentRunResult:
        context = base_context or BaseContext(
            agent="code_agent",
            working_memory=WorkingMemory(
                task=task,
                phase="execute",
                instruction=task,
            ),
        )
        if context.agent != "code_agent":
            raise ValueError("ReActAgent 当前只接受 code_agent BaseContext")

        local = LocalMemoryManager()
        tool_results: list[ToolResult] = []
        context_usages: list[ContextUsage] = []
        phases: list[str] = []

        for step in range(1, self.max_steps + 1):
            # Local Memory 会在每轮工具交互后增长，因此每一轮都重新组装并监控。
            prepared = self.context_runtime.prepare(
                base_context=context,
                local_memory=local.memory,
                registry=self.dispatcher.registry,
            )
            context = prepared.base_context
            local.replace(prepared.local_memory)
            context_usages.append(prepared.usage)
            phases.append(context.working_memory.phase)
            response = self.model.complete(
                messages=prepared.messages,
                tool_schemas=prepared.tool_view.schemas,
            )

            if response.tool_calls:
                assistant_message = self._canonical_assistant_message(response)
                tool_messages: list[dict[str, Any]] = []
                step_results: list[ToolResult] = []
                for model_tool_call in response.tool_calls:
                    result = self._execute_exposed_tool_call(
                        tool_view=prepared.tool_view,
                        tool_call_id=model_tool_call.tool_call_id,
                        name=model_tool_call.name,
                        arguments_json=model_tool_call.arguments_json,
                    )
                    step_results.append(result)
                    tool_results.append(result)
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.tool_call_id,
                            "content": result.model_dump_json(),
                        }
                    )
                local.append(
                    interaction_id=f"step-{step}",
                    assistant_message=assistant_message,
                    tool_messages=tool_messages,
                )
                next_phase = self._next_phase(
                    context.working_memory.phase,
                    step_results,
                )
                if next_phase != context.working_memory.phase:
                    context = context.model_copy(
                        update={
                            "working_memory": context.working_memory.model_copy(
                                update={"phase": next_phase}
                            )
                        }
                    )
                continue

            if response.final_text is not None:
                return AgentRunResult(
                    final_text=response.final_text,
                    steps=step,
                    tool_results=tuple(tool_results),
                    messages=tuple(
                        [*prepared.messages, response.assistant_message]
                    ),
                    context_usages=tuple(context_usages),
                    phases=tuple(phases),
                )

            raise RuntimeError("模型既没有返回 ToolCall，也没有最终文本")

        raise MaxStepsExceeded(f"Agent 超过最大步数：{self.max_steps}")

    def _execute_exposed_tool_call(
        self,
        *,
        tool_view: ToolView,
        tool_call_id: str,
        name: str,
        arguments_json: str,
    ) -> ToolResult:
        if not ToolExposureGuard.allows(name, tool_view.exposed_names):
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=name,
                ok=False,
                error=ToolError(
                    code="tool_not_exposed",
                    message=f"工具未在本轮 Tool View 暴露：{name}",
                ),
            )
        return self._execute_tool_call(
            tool_call_id=tool_call_id,
            name=name,
            arguments_json=arguments_json,
        )

    @staticmethod
    def _canonical_assistant_message(response: Any) -> dict[str, Any]:
        """确保测试模型和不同供应商都给出可配对的 ToolCall 消息。"""

        message = dict(response.assistant_message)
        raw_calls = message.get("tool_calls")
        raw_ids = (
            [item.get("id") for item in raw_calls if isinstance(item, dict)]
            if isinstance(raw_calls, list)
            else []
        )
        expected_ids = [call.tool_call_id for call in response.tool_calls]
        if raw_ids != expected_ids:
            message["tool_calls"] = [
                {
                    "id": call.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_json,
                    },
                }
                for call in response.tool_calls
            ]
        return message

    @staticmethod
    def _next_phase(
        current_phase: str,
        results: list[ToolResult],
    ) -> str:
        """同一次 run 内命令失败进入 debugging，但保留 LocalMemory。"""

        for result in results:
            if result.tool_name != "run_command" or not isinstance(
                result.output, dict
            ):
                continue
            if result.output.get("timed_out") or result.output.get("exit_code") not in (
                None,
                0,
            ):
                return "debugging"
        return current_phase

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
