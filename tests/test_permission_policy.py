"""结构化 argv Permission Policy 测试。"""

import sys

import pytest

from tikiagent.harness import (
    ExecutionContext,
    ExecutionScope,
    RuleBasedPermissionPolicy,
    ValidatedToolCall,
)


def context() -> ExecutionContext:
    return ExecutionContext(
        scope=ExecutionScope(
            task_id="task-1",
            session_id="session-1",
            workspace_id="workspace-1",
        ),
        agent="code_agent",
        exposed_tools={"read_file", "run_command"},
    )


def command_call(command: list[str]) -> ValidatedToolCall:
    return ValidatedToolCall(
        tool_call_id="call-command",
        name="run_command",
        arguments={"command": command},
    )


def test_workspace_file_tools_are_allowed_but_still_need_runtime_boundary() -> None:
    decision = RuleBasedPermissionPolicy().decide(
        ValidatedToolCall(
            tool_call_id="call-read",
            name="read_file",
            arguments={"path": "notes.txt"},
        ),
        context(),
    )

    assert decision.action == "ALLOW"
    assert decision.rule_id == "tool.read_file.allow"


@pytest.mark.parametrize("module", ["pytest", "unittest"])
def test_python_test_entrypoints_are_allowed(module: str) -> None:
    decision = RuleBasedPermissionPolicy().decide(
        command_call([sys.executable, "-m", module]),
        context(),
    )

    assert decision.action == "ALLOW"
    assert decision.rule_id == "command.test.allow"


@pytest.mark.parametrize(
    "command",
    [
        ["pip", "install", "demo"],
        [sys.executable, "-m", "pip", "install", "demo"],
        ["uv", "add", "demo"],
        ["uv.exe", "pip", "install", "demo"],
        ["git", "commit", "-m", "message"],
    ],
)
def test_environment_and_repository_mutations_require_approval(
    command: list[str],
) -> None:
    decision = RuleBasedPermissionPolicy().decide(command_call(command), context())

    assert decision.action == "ASK"


def test_unclassified_command_is_denied() -> None:
    decision = RuleBasedPermissionPolicy().decide(
        command_call([sys.executable, "-c", "print('arbitrary')"]),
        context(),
    )

    assert decision.action == "DENY"
    assert decision.rule_id == "command.unclassified.deny"


def test_unclassified_tool_is_denied() -> None:
    decision = RuleBasedPermissionPolicy().decide(
        ValidatedToolCall(
            tool_call_id="call-danger",
            name="dangerous_tool",
            arguments={},
        ),
        context(),
    )

    assert decision.action == "DENY"
