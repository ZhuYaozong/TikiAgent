"""运行一个真实 Application Session/Turn 并生成 Demo Validation 视图。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from openai import OpenAIError
from pydantic import ValidationError
import httpx

from tikiagent.application.bootstrap import ApplicationRuntimeFactory
from tikiagent.application.events import EventBus
from tikiagent.application.models import ApplicationOutcome
from tikiagent.demo.collector import DemoEventCollector
from tikiagent.demo.models import DemoRunResult, DemoRunSummary, DemoScenario
from tikiagent.demo.summary import DemoSummaryStore, collect_artifacts, load_trace


class DemoController(Protocol):
    def new_session(self, *, workspace_id: str) -> ApplicationOutcome: ...

    def submit(self, *, session_id: str, user_input: str) -> ApplicationOutcome: ...


ControllerFactory = Callable[[EventBus], DemoController]
_OPERATIONAL_ERRORS = (
    OpenAIError,
    httpx.HTTPError,
    OSError,
    RuntimeError,
    ValidationError,
    ValueError,
)


class DemoRunner:
    """不自动处理 Approval/Recovery；暂停状态原样交还用户。"""

    def __init__(
        self,
        data_dir: str | Path = ".tiki-demo",
        *,
        env_file: str | Path = ".env",
        controller_factory: ControllerFactory | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.env_file = Path(env_file)
        self.controller_factory = controller_factory or self._build_controller

    def run(
        self,
        scenario: DemoScenario,
        *,
        workspace_id: str = "demo-workspace",
    ) -> DemoRunResult:
        demo_run_id = str(uuid4())
        started_at = datetime.now(UTC)
        started_clock = perf_counter()
        bus = EventBus(stream_id=demo_run_id)
        collector = DemoEventCollector()
        bus.subscribe(collector)
        controller = self.controller_factory(bus)

        # 一个 Demo Run 固定只创建一个 Session 并提交一个 Turn。
        session = controller.new_session(workspace_id=workspace_id)
        try:
            outcome = controller.submit(
                session_id=session.session_id,
                user_input=scenario.task,
            )
        except _OPERATIONAL_ERRORS as error:
            # 外部供应商/运行环境失败仍生成可审计结果，但不吞掉编程错误。
            outcome = ApplicationOutcome(
                status="workflow_failed",
                session_id=session.session_id,
                message=f"{type(error).__name__}: {error}",
            )
        finished_at = datetime.now(UTC)
        elapsed = perf_counter() - started_clock

        trace_digest, trace_events = load_trace(self.data_dir, session.session_id)
        artifacts = collect_artifacts(self.data_dir, session.session_id)
        final_message = self._final_message(collector, outcome)
        summary = DemoRunSummary(
            demo_run_id=demo_run_id,
            scenario=scenario.name,
            task=scenario.task,
            status=outcome.status,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed,
            session_id=session.session_id,
            task_id=outcome.task_id,
            run_id=outcome.run_id,
            application=collector.digest(),
            trace=trace_digest,
            artifacts=artifacts,
            final_message=final_message,
            next_action=self._next_action(outcome),
        )
        output = DemoSummaryStore(self.data_dir).write(
            summary,
            application_events=collector.events,
            trace_events=trace_events,
        )
        return DemoRunResult(summary=summary, output_dir=output)

    def _build_controller(self, bus: EventBus) -> DemoController:
        return ApplicationRuntimeFactory(
            self.data_dir,
            env_file=self.env_file,
            event_bus=bus,
        ).build_controller()

    @staticmethod
    def _final_message(
        collector: DemoEventCollector,
        outcome: ApplicationOutcome,
    ) -> str:
        for event in reversed(collector.events):
            if event.event_type == "final_answer":
                return event.message
        return outcome.message

    def _next_action(self, outcome: ApplicationOutcome) -> str | None:
        prefix = (
            f'uv run --locked tikiagent --data-dir "{self.data_dir}" '
            f'--env-file "{self.env_file}"'
        )
        if outcome.status == "awaiting_approval":
            return (
                f"{prefix} resume --session-id {outcome.session_id} "
                f"--request-id {outcome.approval_request_id} "
                f"--expected-revision {outcome.checkpoint_revision} --approve"
            )
        if outcome.status == "recovery_required":
            return (
                f"{prefix} recover --session-id {outcome.session_id} "
                f"--execution-id {outcome.execution_id} "
                f"--expected-revision {outcome.checkpoint_revision} "
                "--action confirmed_not_executed --decided-by <name> "
                "--reason <reason>"
            )
        if outcome.status == "awaiting_reconcile":
            return (
                f"{prefix} reconcile --session-id {outcome.session_id} "
                f"--execution-id {outcome.execution_id} "
                f"--expected-revision {outcome.checkpoint_revision} "
                "--result-file <result.json>"
            )
        return None
