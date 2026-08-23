"""v0.6a1 Gate / Enforce / Isolate 的完全离线演示。"""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import BaseModel, ConfigDict

from tikiagent.harness import (
    ApprovalDecision,
    Dispatcher,
    ExecutionContext,
    ExecutionHarness,
    ExecutionScope,
    RegisteredTool,
    ToolRegistry,
    Workspace,
    build_file_registry,
)
from tikiagent.harness.command_tools import RunCommandArgs


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CountingHandler:
    """计数型模拟 handler，保证演示不会真的安装依赖。"""

    def __init__(self, output: dict[str, Any]) -> None:
        self.calls = 0
        self.output = output

    def __call__(self, **_arguments: Any) -> dict[str, Any]:
        self.calls += 1
        return self.output


def scope(**changes: str) -> ExecutionScope:
    return ExecutionScope(
        task_id=changes.get("task_id", "task-demo"),
        session_id=changes.get("session_id", "session-demo"),
        workspace_id=changes.get("workspace_id", "workspace-demo"),
    )


def context(agent: str, exposed_tools: set[str]) -> ExecutionContext:
    return ExecutionContext(
        scope=scope(),
        agent=agent,
        exposed_tools=exposed_tools,
    )


def install_call(call_id: str, package: str = "demo-package") -> dict[str, Any]:
    return {
        "tool_call_id": call_id,
        "name": "run_command",
        "arguments": {
            "command": [sys.executable, "-m", "pip", "install", package]
        },
    }


def show(title: str, value: Any) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    print(value.model_dump_json(indent=2))


def main() -> None:
    with TemporaryDirectory(prefix="tiki-harness-") as temporary:
        workspace = Workspace(Path(temporary) / "workspace")
        workspace.resolve("notes.txt").write_text(
            "Harness 决定怎样安全执行。\n",
            encoding="utf-8",
        )
        command_handler = CountingHandler(
            {"simulated": True, "message": "没有执行真实安装"}
        )
        dangerous_handler = CountingHandler({"unexpected": True})
        registry: ToolRegistry = build_file_registry(workspace)
        registry.register(
            RegisteredTool(
                "run_command",
                "模拟结构化命令",
                RunCommandArgs,
                command_handler,
            )
        )
        registry.register(
            RegisteredTool(
                "dangerous_tool",
                "用于证明 DENY 的演示工具",
                EmptyArgs,
                dangerous_handler,
            )
        )
        harness = ExecutionHarness(Dispatcher(registry))

        hidden = harness.handle(
            {
                "tool_call_id": "call-hidden",
                "name": "run_command",
                "arguments": {"command": [sys.executable, "-m", "pytest"]},
            },
            context=context("research_agent", {"read_file"}),
        )
        show("1. 未暴露工具在 Permission 前被拒绝", hidden)

        allowed = harness.handle(
            {
                "tool_call_id": "call-read",
                "name": "read_file",
                "arguments": {"path": "notes.txt"},
            },
            context=context("code_agent", {"read_file"}),
        )
        show("2. ALLOW 后仍由 Workspace Runtime 执行", allowed)

        denied = harness.handle(
            {
                "tool_call_id": "call-danger",
                "name": "dangerous_tool",
                "arguments": {},
            },
            context=context("code_agent", {"dangerous_tool"}),
        )
        show("3. DENY 不会触达 handler", denied)

        pending = harness.handle(
            install_call("call-install"),
            context=context("code_agent", {"run_command"}),
        )
        show("4. ASK 返回 awaiting_approval，不返回 ToolResult", pending)
        request = pending.approval_request
        if request is None:  # pragma: no cover - Demo 防线
            raise RuntimeError("演示调用没有进入 Approval Gate")
        approved = harness.handle(
            install_call("call-install"),
            context=context("code_agent", {"run_command"}),
            approval_request=request,
            approval_decision=ApprovalDecision(
                request_id=request.request_id,
                approved=True,
                scope=request.scope,
                fingerprint=request.fingerprint,
            ),
        )
        show("5. 正确批准后才执行模拟 handler", approved)

        scoped_pending = harness.handle(
            install_call("call-scoped"),
            context=context("code_agent", {"run_command"}),
        )
        scoped_request = scoped_pending.approval_request
        if scoped_request is None:  # pragma: no cover - Demo 防线
            raise RuntimeError("Scope 演示没有进入 Approval Gate")
        mismatched = harness.handle(
            install_call("call-scoped"),
            context=context("code_agent", {"run_command"}),
            approval_request=scoped_request,
            approval_decision=ApprovalDecision(
                request_id=scoped_request.request_id,
                approved=True,
                scope=scope(workspace_id="other-workspace"),
                fingerprint=scoped_request.fingerprint,
            ),
        )
        show("6. Approval workspace_id 不匹配时拒绝", mismatched)

        print(
            "\nEnforce 计数：",
            {
                "approved_command_handler_calls": command_handler.calls,
                "denied_dangerous_handler_calls": dangerous_handler.calls,
            },
        )


if __name__ == "__main__":
    main()
