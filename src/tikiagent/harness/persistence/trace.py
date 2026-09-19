"""Trace：只负责审计与可观测性，不参与恢复决策。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4
import json
import os

from pydantic import BaseModel, ConfigDict, Field


_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
}


class TraceEvent(BaseModel):
    """带因果顺序与执行身份的单条事件。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    sequence: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    checkpoint_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    execution_id: str | None = None
    tool_call_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class JsonlTraceStore:
    """追加 JSONL；失败可以导致事件缺失，但不能改变 Checkpoint 语义。"""

    def __init__(self, path: str | Path, *, max_text_length: int = 4000) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_text_length = max_text_length
        self._lock = RLock()

    def append(
        self,
        *,
        run_id: str,
        event_type: str,
        checkpoint_id: str | None = None,
        revision: int | None = None,
        execution_id: str | None = None,
        tool_call_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> TraceEvent:
        with self._lock:
            event = TraceEvent(
                sequence=self._last_sequence() + 1,
                run_id=run_id,
                event_type=event_type,
                checkpoint_id=checkpoint_id,
                revision=revision,
                execution_id=execution_id,
                tool_call_id=tool_call_id,
                details=_redact(details or {}, self.max_text_length),
            )
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(event.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return event

    def list_events(self) -> list[TraceEvent]:
        if not self.path.exists():
            return []
        events: list[TraceEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(TraceEvent.model_validate_json(line))
        return events

    def write_views(self, output_dir: str | Path) -> tuple[Path, Path]:
        """生成机器摘要和人工 timeline；两者都不是 Resume 输入。"""

        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        events = self.list_events()
        counts: dict[str, int] = {}
        for event in events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
        summary_path = output / "summary.json"
        summary_path.write_text(
            json.dumps(
                {"event_count": len(events), "event_types": counts},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        timeline_path = output / "timeline.md"
        lines = ["# Trace Timeline", ""]
        lines.extend(
            f"{event.sequence}. `{event.event_type}` "
            f"checkpoint={event.checkpoint_id or '-'} "
            f"execution={event.execution_id or '-'}"
            for event in events
        )
        timeline_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return summary_path, timeline_path

    def _last_sequence(self) -> int:
        events = self.list_events()
        return events[-1].sequence if events else 0


def _redact(value: Any, max_text_length: int, key: str | None = None) -> Any:
    if key is not None and key.casefold() in _SECRET_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _redact(item, max_text_length, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, max_text_length) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, max_text_length) for item in value]
    if isinstance(value, str) and len(value) > max_text_length:
        return value[:max_text_length] + "...[trace truncated]"
    return value
