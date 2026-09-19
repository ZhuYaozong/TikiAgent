"""DemoRunner 单次执行、暂停和结果落盘测试。"""

from pathlib import Path

from tikiagent.application.events import EventBus
from tikiagent.application.models import ApplicationOutcome, EventScope
from tikiagent.demo.runner import DemoRunner
from tikiagent.demo.scenarios import get_scenario
from tikiagent.harness.persistence.trace import JsonlTraceStore


class FakeDemoController:
    def __init__(
        self,
        bus: EventBus,
        data_dir: Path,
        *,
        paused: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.bus = bus
        self.data_dir = data_dir
        self.paused = paused
        self.error = error
        self.new_session_calls = 0
        self.submit_calls = 0

    def new_session(self, *, workspace_id: str) -> ApplicationOutcome:
        self.new_session_calls += 1
        self.bus.emit(
            "session_started",
            scope=EventScope(session_id="session-1", workspace_id=workspace_id),
            source="application_controller",
            correlation_id="session-1",
            message="Session 已创建",
        )
        return ApplicationOutcome(
            status="session_created",
            session_id="session-1",
            message="Session 已创建",
        )

    def submit(self, *, session_id: str, user_input: str) -> ApplicationOutcome:
        self.submit_calls += 1
        if self.error is not None:
            raise self.error
        scope = EventScope(
            session_id=session_id,
            workspace_id="demo-workspace",
            task_id="task-1",
            run_id="run-1",
        )
        self.bus.emit(
            "tool_call_requested",
            scope=scope,
            source="code_agent",
            correlation_id="task-1",
            message="请求 write_file",
        )
        if self.paused:
            self.bus.emit(
                "approval_required",
                scope=scope,
                source="harness_adapter",
                correlation_id="task-1",
                message="等待批准",
            )
            return ApplicationOutcome(
                status="awaiting_approval",
                session_id=session_id,
                task_id="task-1",
                run_id="run-1",
                checkpoint_id="checkpoint-1",
                checkpoint_revision=3,
                approval_request_id="approval-1",
                message="等待批准",
            )
        workspace = self.data_dir / "workspaces" / session_id
        workspace.mkdir(parents=True)
        (workspace / "calculator.py").write_text("def add(a, b): return a + b\n")
        JsonlTraceStore(self.data_dir / "traces" / f"{session_id}.jsonl").append(
            run_id="run-1",
            event_type="tool_execution_finished",
            tool_call_id="tool-1",
        )
        self.bus.emit(
            "final_answer",
            scope=scope,
            source="application_controller",
            correlation_id="task-1",
            message="计算器已完成",
        )
        return ApplicationOutcome(
            status="workflow_completed",
            session_id=session_id,
            task_id="task-1",
            run_id="run-1",
            message="计算器已完成",
        )


def test_runner_executes_one_session_and_one_turn(tmp_path: Path) -> None:
    created: list[FakeDemoController] = []

    def factory(bus: EventBus) -> FakeDemoController:
        controller = FakeDemoController(bus, tmp_path)
        created.append(controller)
        return controller

    result = DemoRunner(tmp_path, controller_factory=factory).run(
        get_scenario("coding")
    )

    assert created[0].new_session_calls == 1
    assert created[0].submit_calls == 1
    assert result.summary.status == "workflow_completed"
    assert result.summary.artifacts == ["calculator.py"]
    assert result.summary.trace.event_count == 1
    assert result.summary.next_action is None
    assert (result.output_dir / "demo-result.md").exists()


def test_runner_preserves_pause_without_auto_resume(tmp_path: Path) -> None:
    created: list[FakeDemoController] = []

    def factory(bus: EventBus) -> FakeDemoController:
        controller = FakeDemoController(bus, tmp_path, paused=True)
        created.append(controller)
        return controller

    result = DemoRunner(tmp_path, controller_factory=factory).run(
        get_scenario("coding")
    )

    assert created[0].submit_calls == 1
    assert result.summary.status == "awaiting_approval"
    assert result.summary.trace.event_count == 0
    assert "resume --session-id session-1" in result.summary.next_action
    assert "--request-id approval-1" in result.summary.next_action
    assert "--expected-revision 3 --approve" in result.summary.next_action


def test_runner_persists_operational_failure_without_traceback(tmp_path: Path) -> None:
    def factory(bus: EventBus) -> FakeDemoController:
        return FakeDemoController(
            bus,
            tmp_path,
            error=RuntimeError("model quota exhausted"),
        )

    result = DemoRunner(tmp_path, controller_factory=factory).run(
        get_scenario("research")
    )

    assert result.summary.status == "workflow_failed"
    assert result.summary.application.event_count == 1
    assert result.summary.trace.event_count == 0
    assert "RuntimeError: model quota exhausted" in result.summary.final_message
    assert (result.output_dir / "demo-result.md").exists()
