"""Demo 产物收集与双时间线持久化。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4
import json
import os

from tikiagent.application.models import ApplicationEvent
from tikiagent.demo.models import DemoRunSummary, TraceDigest
from tikiagent.harness.persistence.trace import JsonlTraceStore, TraceEvent


def collect_artifacts(
    data_dir: str | Path,
    session_id: str,
    *,
    max_files: int = 500,
) -> list[str]:
    """只收集 Session Workspace 内普通文件的相对路径，不读取文件内容。"""

    workspace = Path(data_dir).resolve() / "workspaces" / session_id
    if not workspace.is_dir():
        return []
    artifacts: list[str] = []
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        artifacts.append(path.relative_to(workspace).as_posix())
        if len(artifacts) >= max_files:
            break
    return artifacts


def load_trace(
    data_dir: str | Path,
    session_id: str,
) -> tuple[TraceDigest, list[TraceEvent]]:
    """Trace 只用于展示；缺失 Trace 不影响 Application Outcome。"""

    root = Path(data_dir).resolve()
    path = root / "traces" / f"{session_id}.jsonl"
    if not path.exists():
        return TraceDigest(event_count=0, event_types={}, trace_ref=None), []
    events = JsonlTraceStore(path).list_events()
    counts: dict[str, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return (
        TraceDigest(
            event_count=len(events),
            event_types=counts,
            trace_ref=path.relative_to(root).as_posix(),
        ),
        events,
    )


class DemoSummaryStore:
    """以原子替换写入单次 Demo 的稳定结果视图。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).resolve()

    def write(
        self,
        summary: DemoRunSummary,
        *,
        application_events: Iterable[ApplicationEvent],
        trace_events: Iterable[TraceEvent],
    ) -> Path:
        output = self.data_dir / "demo-runs" / summary.demo_run_id
        output.mkdir(parents=True, exist_ok=True)
        application_events = list(application_events)
        trace_events = list(trace_events)

        application_view = {
            "demo_run_id": summary.demo_run_id,
            "scenario": summary.scenario,
            "status": summary.status,
            "session_id": summary.session_id,
            "task_id": summary.task_id,
            "run_id": summary.run_id,
            "application": summary.application.model_dump(mode="json"),
            "artifacts": summary.artifacts,
            "final_message": summary.final_message,
            "next_action": summary.next_action,
        }
        self._write_json(output / "application-summary.json", application_view)
        self._write_text(
            output / "application-timeline.md",
            self._application_timeline(application_events),
        )
        self._write_json(
            output / "trace-summary.json",
            summary.trace.model_dump(mode="json"),
        )
        self._write_text(
            output / "trace-timeline.md",
            self._trace_timeline(trace_events),
        )
        self._write_text(output / "demo-result.md", self._result_markdown(summary))
        return output

    @staticmethod
    def _application_timeline(events: list[ApplicationEvent]) -> str:
        lines = ["# Application Event Timeline", ""]
        if not events:
            lines.append("本次运行没有 Application Event。")
        lines.extend(
            f"{event.sequence}. `{event.event_type}` [{event.source}] {event.message}"
            for event in events
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _trace_timeline(events: list[TraceEvent]) -> str:
        lines = ["# Harness Trace Timeline", ""]
        if not events:
            lines.append("本次运行没有 Harness Trace（Research-only 场景可能出现）。")
        lines.extend(
            f"{event.sequence}. `{event.event_type}` "
            f"checkpoint={event.checkpoint_id or '-'} "
            f"execution={event.execution_id or '-'}"
            for event in events
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _result_markdown(summary: DemoRunSummary) -> str:
        artifact_lines = (
            [f"- `{path}`" for path in summary.artifacts]
            if summary.artifacts
            else ["- 无"]
        )
        lines = [
            f"# Demo Result: {summary.scenario}",
            "",
            f"- Status: `{summary.status}`",
            f"- Session: `{summary.session_id}`",
            f"- Task: `{summary.task_id or '-'}`",
            f"- Run: `{summary.run_id or '-'}`",
            f"- Elapsed: `{summary.elapsed_seconds:.3f}s`",
            f"- Application Events: `{summary.application.event_count}`",
            f"- Harness Trace Events: `{summary.trace.event_count}`",
            "",
            "## Artifacts",
            "",
            *artifact_lines,
            "",
            "## Final Message",
            "",
            summary.final_message,
        ]
        if summary.next_action:
            lines.extend(["", "## Next Action", "", f"`{summary.next_action}`"])
        return "\n".join(lines) + "\n"

    def _write_json(self, path: Path, value: object) -> None:
        self._write_text(
            path,
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
