"""Approval 的 Scope、参数绑定与一次性消费测试。"""

from typing import Any
import sys

import pytest

from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.permissions.models import ApprovalDecision
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.tools.commands import RunCommandArgs
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.registry import RegisteredTool, ToolRegistry


class CountingHandler:
    def __init__(self) -> None:
        self.calls = 0
        self.events: list[str] = []

    def __call__(self, **_arguments: Any) -> dict[str, bool]:
        self.calls += 1
        self.events.append("handler")
        return {"simulated": True}


def build_system() -> tuple[ExecutionHarness, CountingHandler]:
    handler = CountingHandler()
    registry = ToolRegistry()
    registry.register(
        RegisteredTool("run_command", "command", RunCommandArgs, handler)
    )
    return ExecutionHarness(Dispatcher(registry)), handler


def context(**scope_changes: str) -> ExecutionContext:
    values = {
        "task_id": "task-1",
        "session_id": "session-1",
        "workspace_id": "workspace-1",
        **scope_changes,
    }
    return ExecutionContext(
        scope=ExecutionScope(**values),
        agent="code_agent",
        exposed_tools={"run_command"},
    )


def install_call(package: str = "demo") -> dict[str, object]:
    return {
        "tool_call_id": "call-install",
        "name": "run_command",
        "arguments": {
            "command": [sys.executable, "-m", "pip", "install", package]
        },
    }


def request_approval(harness: ExecutionHarness):
    outcome = harness.handle(install_call(), context=context())
    assert outcome.status == "awaiting_approval"
    assert outcome.tool_result is None
    assert outcome.approval_request is not None
    return outcome.approval_request


def approve(request, *, decision_scope: ExecutionScope | None = None):
    return ApprovalDecision(
        request_id=request.request_id,
        approved=True,
        scope=decision_scope or request.scope,
        fingerprint=request.fingerprint,
    )


def test_ask_pauses_then_correct_approval_executes_once() -> None:
    harness, handler = build_system()
    request = request_approval(harness)

    completed = harness.handle(
        install_call(),
        context=context(),
        approval_request=request,
        approval_decision=approve(request),
        before_execute=lambda _call: handler.events.append("before_execute"),
    )
    replayed = harness.handle(
        install_call(),
        context=context(),
        approval_request=request,
        approval_decision=approve(request),
    )

    assert completed.status == "completed"
    assert completed.tool_result.ok is True
    assert replayed.status == "denied"
    assert replayed.tool_result.error.code == "approval_already_resolved"
    assert handler.calls == 1
    assert handler.events == ["before_execute", "handler"]


@pytest.mark.parametrize(
    "scope_changes",
    [
        {"task_id": "other-task"},
        {"session_id": "other-session"},
        {"workspace_id": "other-workspace"},
    ],
)
def test_approval_rejects_task_session_and_workspace_mismatch(
    scope_changes: dict[str, str],
) -> None:
    harness, handler = build_system()
    request = request_approval(harness)
    mismatched_scope = request.scope.model_copy(update=scope_changes)

    outcome = harness.handle(
        install_call(),
        context=context(),
        approval_request=request,
        approval_decision=approve(request, decision_scope=mismatched_scope),
    )

    assert outcome.status == "denied"
    assert outcome.tool_result.error.code == "approval_scope_mismatch"
    assert handler.calls == 0


def test_approval_rejects_current_execution_scope_mismatch() -> None:
    harness, handler = build_system()
    request = request_approval(harness)

    outcome = harness.handle(
        install_call(),
        context=context(workspace_id="other-workspace"),
        approval_request=request,
        approval_decision=approve(request),
    )

    assert outcome.status == "denied"
    assert outcome.tool_result.error.code == "approval_scope_mismatch"
    assert handler.calls == 0


def test_approval_rejects_canonical_argument_tampering() -> None:
    harness, handler = build_system()
    request = request_approval(harness)

    outcome = harness.handle(
        install_call("other-package"),
        context=context(),
        approval_request=request,
        approval_decision=approve(request),
    )

    assert outcome.status == "denied"
    assert outcome.tool_result.error.code == "approval_fingerprint_mismatch"
    assert handler.calls == 0


def test_rejected_approval_is_consumed_without_execution() -> None:
    harness, handler = build_system()
    request = request_approval(harness)
    rejected = ApprovalDecision(
        request_id=request.request_id,
        approved=False,
        scope=request.scope,
        fingerprint=request.fingerprint,
    )

    first = harness.handle(
        install_call(),
        context=context(),
        approval_request=request,
        approval_decision=rejected,
    )
    second = harness.handle(
        install_call(),
        context=context(),
        approval_request=request,
        approval_decision=approve(request),
    )

    assert first.tool_result.error.code == "approval_rejected"
    assert second.tool_result.error.code == "approval_already_resolved"
    assert handler.calls == 0


def test_approval_request_contains_canonical_defaults() -> None:
    harness, _ = build_system()
    request = request_approval(harness)

    assert request.tool_call.arguments["cwd"] == "."
    assert request.tool_call.arguments["timeout_seconds"] == 30.0
    assert request.tool_call.arguments["output_limit"] == 8_000
