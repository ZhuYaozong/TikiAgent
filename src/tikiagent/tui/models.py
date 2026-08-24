"""只服务于显示的 TUI 投影模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


TimelineKind = Literal[
    "user", "routing", "agent", "tool", "approval", "verification", "system", "final"
]


class TuiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimelineItem(TuiModel):
    sequence: int = Field(ge=1)
    kind: TimelineKind
    title: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class TranscriptItem(TuiModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)
    status: str | None = None


class WorkspaceEntry(TuiModel):
    path: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: Literal["directory", "file", "symlink"]
    depth: int = Field(ge=0)


class SessionSnapshot(TuiModel):
    session_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    turn_count: int = Field(ge=0)
    transcript: tuple[TranscriptItem, ...] = ()


class TuiViewState(TuiModel):
    """可随时重建的 UI 投影，不是 Checkpoint、Session 或 Approval Ledger。"""

    stream_id: str | None = None
    last_sequence: int = Field(default=0, ge=0)
    session_id: str | None = None
    workspace_id: str | None = None
    turn_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    status: str = "idle"
    current_agent: str = "-"
    busy: bool = False
    tool_calls: int = Field(default=0, ge=0)
    approvals: int = Field(default=0, ge=0)
    verification: str = "-"
    checkpoint_id: str | None = None
    checkpoint_revision: int | None = Field(default=None, ge=1)
    approval_request_id: str | None = None
    execution_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    tool_call_id: str | None = None
    tool_name: str | None = None
    final_answer: str | None = None
    notice: str | None = None
    error: str | None = None
    transcript: tuple[TranscriptItem, ...] = ()
    timeline: tuple[TimelineItem, ...] = ()
    workspace_entries: tuple[WorkspaceEntry, ...] = ()
