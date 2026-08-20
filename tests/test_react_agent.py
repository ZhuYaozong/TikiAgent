"""ReAct Agent Loop 测试。"""

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tikiagent.agents import MaxStepsExceeded, ReActAgent
from tikiagent.harness import (
    Dispatcher,
    Workspace,
    build_file_registry,
    register_command_tool,
)
from tikiagent.llm import ModelResponse, ModelToolCall


class ScriptedModel:
    """只用于确定性测试，不模拟 Dispatcher 行为。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = responses
        self.requests: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_schemas: Sequence[Mapping[str, Any]],
    ) -> ModelResponse:
        assert {schema["name"] for schema in tool_schemas} >= {
            "read_file",
            "edit_file",
        }
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


def build_agent(
    tmp_path: Path,
    model: ScriptedModel,
    max_steps: int = 8,
) -> tuple[Workspace, ReActAgent]:
    workspace = Workspace(tmp_path / "workspace")
    registry = build_file_registry(workspace)
    register_command_tool(registry, workspace)
    dispatcher = Dispatcher(registry)
    return workspace, ReActAgent(model, dispatcher, max_steps=max_steps)


def test_agent_observes_edits_and_finishes(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call_read", "read_file", {"path": "calculator.py"}),
            tool_response(
                "call_edit",
                "edit_file",
                {
                    "path": "calculator.py",
                    "old_text": "return a - b",
                    "new_text": "return a + b",
                },
            ),
            tool_response("call_verify", "read_file", {"path": "calculator.py"}),
            final_response("修复完成"),
        ]
    )
    workspace, agent = build_agent(tmp_path, model)
    workspace.resolve("calculator.py").write_text(
        "return a - b\n",
        encoding="utf-8",
    )

    result = agent.run("修复 calculator.py")

    assert result.final_text == "修复完成"
    assert result.steps == 4
    assert [item.ok for item in result.tool_results] == [True, True, True]
    assert workspace.resolve("calculator.py").read_text(
        encoding="utf-8"
    ) == "return a + b\n"
    assert model.requests[1][-1]["tool_call_id"] == "call_read"


def test_invalid_json_becomes_an_observation(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call_bad", "read_file", "{not-json"),
            final_response("参数不合法，任务停止"),
        ]
    )
    _, agent = build_agent(tmp_path, model)

    result = agent.run("读取文件")

    assert result.tool_results[0].ok is False
    assert result.tool_results[0].error is not None
    assert result.tool_results[0].error.code == "invalid_arguments_json"
    observation = json.loads(model.requests[1][-1]["content"])
    assert observation["error"]["code"] == "invalid_arguments_json"


def test_agent_stops_at_max_steps(tmp_path: Path) -> None:
    model = ScriptedModel(
        [
            tool_response("call_1", "read_file", {"path": "missing.txt"}),
            tool_response("call_2", "read_file", {"path": "missing.txt"}),
        ]
    )
    _, agent = build_agent(tmp_path, model, max_steps=2)

    with pytest.raises(MaxStepsExceeded):
        agent.run("不断读取不存在的文件")


def test_agent_repairs_code_and_verifies_with_real_process(
    tmp_path: Path,
) -> None:
    test_command = [
        sys.executable,
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        ".",
        "-p",
        "test_*.py",
    ]
    model = ScriptedModel(
        [
            tool_response(
                "call_baseline",
                "run_command",
                {"command": test_command},
            ),
            tool_response(
                "call_edit",
                "edit_file",
                {
                    "path": "calculator.py",
                    "old_text": "return a - b",
                    "new_text": "return a + b",
                },
            ),
            tool_response(
                "call_verify",
                "run_command",
                {"command": test_command},
            ),
            final_response("修复完成，测试通过"),
        ]
    )
    workspace, agent = build_agent(tmp_path, model)
    workspace.resolve("calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a - b\n",
        encoding="utf-8",
    )
    workspace.resolve("test_calculator.py").write_text(
        "import unittest\n"
        "from calculator import add\n\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )

    result = agent.run("修复代码并运行测试")

    assert result.final_text == "修复完成，测试通过"
    assert result.tool_results[0].ok is True
    assert result.tool_results[0].output["exit_code"] == 1
    assert result.tool_results[2].ok is True
    assert result.tool_results[2].output["exit_code"] == 0
    baseline_observation = json.loads(model.requests[1][-1]["content"])
    assert baseline_observation["output"]["exit_code"] == 1
