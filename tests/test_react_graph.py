"""LangGraph ReAct 路由、Reducer 与终止条件测试。"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tikiagent.harness import Dispatcher, Workspace, build_file_registry
from tikiagent.llm import ModelResponse, ModelToolCall
from tikiagent.orchestration import ReActWorkflow


class ScriptedModel:
    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_schemas: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        assert any(schema["name"] == "read_file" for schema in tool_schemas)
        self.requests.append([dict(message) for message in messages])
        return self.responses.pop(0)


def tool_response(
    call_id: str,
    name: str,
    arguments: dict[str, Any] | str,
) -> ModelResponse:
    arguments_json = (
        arguments if isinstance(arguments, str) else json.dumps(arguments)
    )
    return ModelResponse(
        assistant_message={
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments_json,
                    },
                }
            ],
        },
        tool_calls=(ModelToolCall(call_id, name, arguments_json),),
    )


def final_response(text: str) -> ModelResponse:
    return ModelResponse(
        assistant_message={"role": "assistant", "content": text},
        final_text=text,
    )


def build_workflow(
    tmp_path: Path,
    model: ScriptedModel,
    *,
    max_steps: int = 8,
) -> tuple[Workspace, ReActWorkflow]:
    workspace = Workspace(tmp_path / "workspace")
    dispatcher = Dispatcher(build_file_registry(workspace))
    workflow = ReActWorkflow(
        model=model,
        dispatcher=dispatcher,
        workspace_id="test-workspace",
        max_steps=max_steps,
    )
    return workspace, workflow


def test_graph_appends_messages_and_routes_tools_back_to_actor(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(
        [
            tool_response("call_read", "read_file", {"path": "demo.txt"}),
            final_response("读取完成"),
        ]
    )
    workspace, workflow = build_workflow(tmp_path, model)
    workspace.resolve("demo.txt").write_text("hello", encoding="utf-8")

    snapshots = list(
        workflow.stream("读取 demo.txt", session_id="session-graph")
    )
    final_state = snapshots[-1]

    assert final_state["status"] == "completed"
    assert final_state["final_result"] == "读取完成"
    assert final_state["session_id"] == "session-graph"
    assert final_state["workspace_id"] == "test-workspace"
    assert final_state["step_count"] == 2
    assert len(final_state["messages"]) == 5
    assert len(final_state["tool_results"]) == 1
    assert final_state["pending_tool_calls"] == []
    assert model.requests[1][-1]["tool_call_id"] == "call_read"


def test_tool_failure_is_preserved_as_graph_observation(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response(
                "call_missing",
                "read_file",
                {"path": "missing.txt"},
            ),
            final_response("文件不存在，任务结束"),
        ]
    )
    _, workflow = build_workflow(tmp_path, model)

    state = workflow.invoke("读取缺失文件")

    assert state["status"] == "completed"
    assert state["tool_results"][0]["ok"] is False
    assert state["tool_results"][0]["error"]["code"] == "file_not_found"
    observation = json.loads(model.requests[1][-1]["content"])
    assert observation["error"]["code"] == "file_not_found"


def test_invalid_tool_arguments_json_becomes_observation(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call_bad", "read_file", "{invalid-json"),
            final_response("参数错误"),
        ]
    )
    _, workflow = build_workflow(tmp_path, model)

    state = workflow.invoke("读取文件")

    error = state["tool_results"][0]["error"]
    assert error["code"] == "invalid_arguments_json"


def test_business_max_steps_returns_final_state(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call_1", "read_file", {"path": "missing.txt"}),
            tool_response("call_2", "read_file", {"path": "missing.txt"}),
        ]
    )
    _, workflow = build_workflow(tmp_path, model, max_steps=2)

    state = workflow.invoke("不断读取缺失文件")

    assert state["status"] == "max_steps"
    assert state["step_count"] == 2
    assert len(state["tool_results"]) == 2
    assert "2" in state["final_result"]


def test_compiled_graph_has_explicit_actor_and_tools_nodes(
    tmp_path: Path,
) -> None:
    _, workflow = build_workflow(
        tmp_path,
        ScriptedModel([final_response("done")]),
    )

    node_names = set(workflow.graph.get_graph().nodes)

    assert {"__start__", "actor", "tools", "__end__"} <= node_names


def test_recursion_limit_must_be_positive(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    dispatcher = Dispatcher(build_file_registry(workspace))

    with pytest.raises(ValueError, match="recursion_limit"):
        ReActWorkflow(
            model=ScriptedModel([final_response("done")]),
            dispatcher=dispatcher,
            workspace_id="test-workspace",
            recursion_limit=0,
        )
