"""Demo 双视图、产物清单与脱敏边界测试。"""

from datetime import UTC, datetime
from pathlib import Path

from tikiagent.application.events import EventBus
from tikiagent.application.models import EventScope
from tikiagent.demo.collector import DemoEventCollector
from tikiagent.demo.models import DemoRunSummary
from tikiagent.demo.summary import DemoSummaryStore, collect_artifacts, load_trace
from tikiagent.harness.trace import JsonlTraceStore


def test_summary_writes_separate_sanitized_application_and_trace_views(
    tmp_path: Path,
) -> None:
    bus = EventBus(stream_id="demo-1")
    collector = DemoEventCollector()
    bus.subscribe(collector)
    scope = EventScope(session_id="session-1", task_id="task-1")
    bus.emit(
        "agent_started",
        scope=scope,
        source="workflow_adapter",
        correlation_id="task-1",
        message="进入 code_agent",
        data={"agent": "code_agent", "api_key": "application-secret"},
    )
    bus.emit(
        "verification_completed",
        scope=scope,
        source="workflow_adapter",
        correlation_id="result-1",
        message="verification passed=True",
        data={"passed": True},
    )
    trace_store = JsonlTraceStore(tmp_path / "traces" / "session-1.jsonl")
    trace_store.append(
        run_id="run-1",
        event_type="tool_execution_started",
        execution_id="execution-1",
        details={"token": "trace-secret", "command": "python -m unittest"},
    )
    trace_digest, trace_events = load_trace(tmp_path, "session-1")
    summary = DemoRunSummary(
        demo_run_id="demo-1",
        scenario="coding",
        task="创建并测试计算器",
        status="workflow_completed",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        elapsed_seconds=0.1,
        session_id="session-1",
        task_id="task-1",
        run_id="run-1",
        application=collector.digest(),
        trace=trace_digest,
        artifacts=[],
        final_message="完成",
    )

    output = DemoSummaryStore(tmp_path).write(
        summary,
        application_events=collector.events,
        trace_events=trace_events,
    )

    expected = {
        "application-summary.json",
        "application-timeline.md",
        "trace-summary.json",
        "trace-timeline.md",
        "demo-result.md",
    }
    assert {path.name for path in output.iterdir()} == expected
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir()
    )
    assert "application-secret" not in combined
    assert "trace-secret" not in combined
    assert "tool_execution_started" in combined
    assert summary.application.verification_passed == 1


def test_artifacts_are_relative_and_missing_trace_is_valid(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "session-1"
    (workspace / "site").mkdir(parents=True)
    (workspace / "site" / "index.html").write_text("ok", encoding="utf-8")

    digest, events = load_trace(tmp_path, "session-1")

    assert collect_artifacts(tmp_path, "session-1") == ["site/index.html"]
    assert digest.event_count == 0
    assert digest.trace_ref is None
    assert events == []
