"""由 LangGraph 显式编排的 ReAct 工作流。"""

import json
from collections.abc import Iterator
from typing import Any, Literal, cast

from langgraph.graph import END, START, StateGraph

from tikiagent.agents.react import DEFAULT_SYSTEM_PROMPT
from tikiagent.harness.dispatcher import Dispatcher
from tikiagent.harness.models import ToolError, ToolResult
from tikiagent.llm.models import ModelClient
from tikiagent.orchestration.state import (
    PendingToolCall,
    TikiState,
    create_initial_state,
)


class ReActWorkflow:
    """把 Actor、Tools、路由和状态更新编译成可运行图。"""

    def __init__(
        self,
        *,
        model: ModelClient,
        dispatcher: Dispatcher,
        workspace_id: str,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 8,
        recursion_limit: int | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 必须大于 0")
        if recursion_limit is not None and recursion_limit < 1:
            raise ValueError("recursion_limit 必须大于 0")

        self.model = model
        self.dispatcher = dispatcher
        self.workspace_id = workspace_id
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.recursion_limit = (
            recursion_limit
            if recursion_limit is not None
            else max_steps * 2 + 4
        )
        self.graph = self._build_graph()

    def _actor_node(self, state: TikiState) -> dict[str, Any]:
        """请求模型决定调用工具或生成最终回答。"""

        if state["step_count"] >= state["max_steps"]:
            return {
                "status": "max_steps",
                "final_result": (
                    "Agent 达到最大模型调用次数："
                    f"{state['max_steps']}"
                ),
                "pending_tool_calls": [],
            }

        response = self.model.complete(
            messages=state["messages"],
            tool_schemas=self.dispatcher.registry.schemas(),
        )
        pending_tool_calls: list[PendingToolCall] = [
            {
                "tool_call_id": tool_call.tool_call_id,
                "name": tool_call.name,
                "arguments_json": tool_call.arguments_json,
            }
            for tool_call in response.tool_calls
        ]
        update: dict[str, Any] = {
            "messages": [response.assistant_message],
            "pending_tool_calls": pending_tool_calls,
            "step_count": state["step_count"] + 1,
        }

        if pending_tool_calls:
            update["status"] = "running"
            return update

        if response.final_text is None:
            update.update(
                status="failed",
                final_result="模型既没有返回 ToolCall，也没有最终文本",
            )
            return update

        update.update(
            status="completed",
            final_result=response.final_text,
        )
        return update

    def _tools_node(self, state: TikiState) -> dict[str, Any]:
        """执行当前 pending_tool_calls，并生成 Tool Messages。"""

        tool_messages: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for pending_call in state["pending_tool_calls"]:
            result = self._dispatch_pending_call(pending_call)
            tool_results.append(result.model_dump(mode="json"))
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": result.model_dump_json(),
                }
            )

        return {
            "messages": tool_messages,
            "tool_results": tool_results,
            "pending_tool_calls": [],
        }

    def _dispatch_pending_call(
        self,
        pending_call: PendingToolCall,
    ) -> ToolResult:
        try:
            arguments = json.loads(pending_call["arguments_json"])
        except json.JSONDecodeError as error:
            return ToolResult(
                tool_call_id=pending_call["tool_call_id"],
                tool_name=pending_call["name"],
                ok=False,
                error=ToolError(
                    code="invalid_arguments_json",
                    message="模型生成的工具参数不是合法 JSON",
                    details={
                        "arguments": pending_call["arguments_json"],
                        "error": str(error),
                    },
                ),
            )

        return self.dispatcher.dispatch(
            {
                "tool_call_id": pending_call["tool_call_id"],
                "name": pending_call["name"],
                "arguments": arguments,
            }
        )

    @staticmethod
    def _route_after_actor(
        state: TikiState,
    ) -> Literal["tools", "end"]:
        if state["status"] == "running" and state["pending_tool_calls"]:
            return "tools"
        return "end"

    def _build_graph(self):
        builder = StateGraph(TikiState)
        builder.add_node("actor", self._actor_node)
        builder.add_node("tools", self._tools_node)
        builder.add_edge(START, "actor")
        builder.add_conditional_edges(
            "actor",
            self._route_after_actor,
            {"tools": "tools", "end": END},
        )
        builder.add_edge("tools", "actor")
        return builder.compile()

    def initial_state(
        self,
        task: str,
        *,
        session_id: str | None = None,
    ) -> TikiState:
        return create_initial_state(
            task=task,
            system_prompt=self.system_prompt,
            workspace_id=self.workspace_id,
            max_steps=self.max_steps,
            session_id=session_id,
        )

    def invoke(
        self,
        task: str,
        *,
        session_id: str | None = None,
    ) -> TikiState:
        result = self.graph.invoke(
            self.initial_state(task, session_id=session_id),
            config={"recursion_limit": self.recursion_limit},
        )
        return cast(TikiState, result)

    def stream(
        self,
        task: str,
        *,
        session_id: str | None = None,
    ) -> Iterator[TikiState]:
        snapshots = self.graph.stream(
            self.initial_state(task, session_id=session_id),
            config={"recursion_limit": self.recursion_limit},
            stream_mode="values",
        )
        for snapshot in snapshots:
            yield cast(TikiState, snapshot)
