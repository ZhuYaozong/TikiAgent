"""离线演示 ASK → Checkpoint → 新 Runtime → Approval → Resume。"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from tikiagent.harness import (
    ApprovalDecision,
    Dispatcher,
    ExecutionContext,
    ExecutionCoordinator,
    ExecutionHarness,
    ExecutionScope,
    HistoryResumeReference,
    JsonCheckpointStore,
    JsonlTraceStore,
    PendingModelToolCall,
    PermissionDecision,
    ReActRunSnapshot,
    RegisteredTool,
    ToolRegistry,
    WorkflowResumeSnapshot,
)


class WriteMarkerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


class AlwaysAsk:
    def decide(self, _call, _context) -> PermissionDecision:
        return PermissionDecision(
            action="ASK",
            rule_id="demo.always_ask",
            reason="演示有副作用工具的人工审批",
        )


def build_coordinator(root: Path) -> ExecutionCoordinator:
    registry = ToolRegistry()

    def write_marker(content: str) -> dict[str, str]:
        path = root / "marker.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": str(path)}

    registry.register(
        RegisteredTool(
            "write_marker",
            "写入演示标记文件",
            WriteMarkerArgs,
            write_marker,
        )
    )
    return ExecutionCoordinator(
        ExecutionHarness(
            Dispatcher(registry),
            permission_policy=AlwaysAsk(),
        ),
        JsonCheckpointStore(root / "checkpoints"),
        JsonlTraceStore(root / "events.jsonl"),
    )


def main() -> None:
    root = Path(".tiki") / "harness-resume-demo"
    context = ExecutionContext(
        scope=ExecutionScope(
            task_id="demo-task",
            session_id="demo-session",
            workspace_id="demo-workspace",
        ),
        agent="code_agent",
        exposed_tools={"write_marker"},
    )
    workflow = WorkflowResumeSnapshot(
        state={"task_id": "demo-task", "status": "executing"},
        history=HistoryResumeReference(
            path=str(root / "history.jsonl"),
            cursor=0,
        ),
    )
    react = ReActRunSnapshot(
        task="写入 marker",
        step=1,
        base_context={"agent": "code_agent"},
        local_memory={"summary": None, "recent_interactions": []},
        pending_assistant_message={"role": "assistant"},
        pending_tool_calls=[
            PendingModelToolCall(
                tool_call_id="demo-call",
                name="write_marker",
                arguments_json='{"content":"resumed"}',
            )
        ],
        next_tool_index=0,
    )

    first_runtime = build_coordinator(root)
    paused = first_runtime.execute(
        {
            "tool_call_id": "demo-call",
            "name": "write_marker",
            "arguments": {"content": "resumed"},
        },
        context=context,
        workflow_snapshot=workflow,
        react_snapshot=react,
        run_id="demo-run",
    )
    checkpoint = paused.checkpoint
    request = paused.outcome.approval_request
    assert checkpoint is not None and request is not None
    print(
        f"[Paused] checkpoint={checkpoint.checkpoint_id} "
        f"revision={checkpoint.revision}"
    )

    # 重建 Coordinator 模拟新进程；恢复依据只来自 Checkpoint。
    second_runtime = build_coordinator(root)
    completed = second_runtime.resume_approval(
        checkpoint.checkpoint_id,
        decision=ApprovalDecision(
            request_id=request.request_id,
            approved=True,
            scope=request.scope,
            fingerprint=request.fingerprint,
        ),
        expected_revision=checkpoint.revision,
    )
    print(
        f"[Completed] state={completed.checkpoint.execution_state} "
        f"revision={completed.checkpoint.revision}"
    )
    second_runtime.trace_store.write_views(root)


if __name__ == "__main__":
    main()
