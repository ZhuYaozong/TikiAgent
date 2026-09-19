"""ExecutionHarness 固定顺序、Enforce 与 Runtime 边界测试。"""

from typing import Any
import sys

from pydantic import BaseModel, ConfigDict, ValidationError
import pytest

from tikiagent.context.memory.models import ReActInteraction
from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.models import HarnessOutcome
from tikiagent.harness.permissions.models import PermissionDecision
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.harness.workspace import Workspace
from tikiagent.tools.commands import RunCommandArgs
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.files import build_file_registry
from tikiagent.tools.models import ToolResult
from tikiagent.tools.registry import RegisteredTool, ToolRegistry


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CountingHandler:
    def __init__(self, output: Any = None) -> None:
        self.calls = 0
        self.output = output
        self.events: list[str] = []

    def __call__(self, **_arguments: Any) -> Any:
        self.calls += 1
        self.events.append("handler")
        return self.output


class SpyPermissionPolicy:
    def __init__(self, action: str) -> None:
        self.action = action
        self.calls = 0

    def decide(self, _tool_call, _context) -> PermissionDecision:
        self.calls += 1
        return PermissionDecision(
            action=self.action,
            rule_id=f"spy.{self.action.lower()}",
            reason=f"spy {self.action}",
        )


def execution_context(*exposed_tools: str) -> ExecutionContext:
    return ExecutionContext(
        scope=ExecutionScope(
            task_id="task-1",
            session_id="session-1",
            workspace_id="workspace-1",
        ),
        agent="code_agent",
        exposed_tools=set(exposed_tools),
    )


def build_harness(
    tool: RegisteredTool,
    *,
    policy=None,
) -> ExecutionHarness:
    registry = ToolRegistry()
    registry.register(tool)
    return ExecutionHarness(
        Dispatcher(registry),
        permission_policy=policy,
    )


def test_exposure_guard_runs_before_prepare_and_permission() -> None:
    handler = CountingHandler()
    policy = SpyPermissionPolicy("ALLOW")
    harness = build_harness(
        RegisteredTool("hidden_tool", "hidden", EmptyArgs, handler),
        policy=policy,
    )

    outcome = harness.handle(
        {
            "tool_call_id": "call-hidden",
            "name": "hidden_tool",
            # 参数即使非法，也必须先得到 tool_not_exposed。
            "arguments": {"unexpected": True},
        },
        context=execution_context(),
    )

    assert outcome.status == "denied"
    assert outcome.tool_result.error.code == "tool_not_exposed"
    assert policy.calls == 0
    assert handler.calls == 0


def test_dispatcher_prepare_canonicalizes_defaults_without_execution() -> None:
    handler = CountingHandler()
    registry = ToolRegistry()
    registry.register(
        RegisteredTool("run_command", "command", RunCommandArgs, handler)
    )
    dispatcher = Dispatcher(registry)

    prepared = dispatcher.prepare(
        {
            "tool_call_id": "call-command",
            "name": "run_command",
            "arguments": {"command": [sys.executable, "-m", "pytest"]},
        }
    )

    assert not isinstance(prepared, ToolResult)
    assert prepared.arguments["cwd"] == "."
    assert prepared.arguments["timeout_seconds"] == 30.0
    assert handler.calls == 0


def test_permission_deny_never_calls_handler() -> None:
    handler = CountingHandler()
    harness = build_harness(
        RegisteredTool("dangerous_tool", "dangerous", EmptyArgs, handler)
    )

    outcome = harness.handle(
        {
            "tool_call_id": "call-danger",
            "name": "dangerous_tool",
            "arguments": {},
        },
        context=execution_context("dangerous_tool"),
    )

    assert outcome.status == "denied"
    assert outcome.tool_result.error.code == "permission_denied"
    assert handler.calls == 0


def test_before_execute_runs_after_allow_and_before_handler() -> None:
    handler = CountingHandler({"ok": True})
    policy = SpyPermissionPolicy("ALLOW")
    harness = build_harness(
        RegisteredTool("safe_tool", "safe", EmptyArgs, handler),
        policy=policy,
    )

    def before_execute(_call) -> None:
        handler.events.append("before_execute")

    outcome = harness.handle(
        {
            "tool_call_id": "call-safe",
            "name": "safe_tool",
            "arguments": {},
        },
        context=execution_context("safe_tool"),
        before_execute=before_execute,
    )

    assert outcome.status == "completed"
    assert handler.events == ["before_execute", "handler"]


def test_runtime_boundary_failure_is_completed_tool_observation(tmp_path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    harness = ExecutionHarness(Dispatcher(build_file_registry(workspace)))

    outcome = harness.handle(
        {
            "tool_call_id": "call-read",
            "name": "read_file",
            "arguments": {"path": "../../outside.txt"},
        },
        context=execution_context("read_file"),
    )

    # Approval workspace_id 不负责判断文件路径；真实 Workspace 在 Runtime 拦截。
    assert outcome.status == "completed"
    assert outcome.tool_result.ok is False
    assert outcome.tool_result.error.code == "workspace_escape"


def test_shell_string_is_rejected_before_permission() -> None:
    handler = CountingHandler()
    policy = SpyPermissionPolicy("ALLOW")
    harness = build_harness(
        RegisteredTool("run_command", "command", RunCommandArgs, handler),
        policy=policy,
    )

    outcome = harness.handle(
        {
            "tool_call_id": "call-shell",
            "name": "run_command",
            "arguments": {"command": "python -m pytest && arbitrary"},
        },
        context=execution_context("run_command"),
    )

    assert outcome.status == "denied"
    assert outcome.tool_result.error.code == "invalid_arguments"
    assert policy.calls == 0
    assert handler.calls == 0


def test_harness_outcome_rejects_illegal_status_payload_combinations() -> None:
    with pytest.raises(ValidationError, match="completed"):
        HarnessOutcome(status="completed")
    with pytest.raises(ValidationError, match="awaiting_approval"):
        HarnessOutcome(
            status="awaiting_approval",
            tool_result=ToolResult(
                tool_call_id="call",
                tool_name="tool",
                ok=False,
            ),
        )


def test_awaiting_approval_does_not_create_incomplete_local_interaction() -> None:
    handler = CountingHandler()
    harness = build_harness(
        RegisteredTool("run_command", "command", RunCommandArgs, handler)
    )
    outcome = harness.handle(
        {
            "tool_call_id": "call-install",
            "name": "run_command",
            "arguments": {
                "command": [sys.executable, "-m", "pip", "install", "demo"]
            },
        },
        context=execution_context("run_command"),
    )

    assert outcome.status == "awaiting_approval"
    assert outcome.tool_result is None
    assert handler.calls == 0
    # Pending ToolCall 必须留在 Harness Runtime；没有 ToolResult 就不能进入 LocalMemory。
    with pytest.raises(ValidationError):
        ReActInteraction(
            interaction_id="pending-install",
            assistant_message={
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-install",
                        "type": "function",
                        "function": {"name": "run_command", "arguments": "{}"},
                    }
                ],
            },
            tool_messages=[],
        )
