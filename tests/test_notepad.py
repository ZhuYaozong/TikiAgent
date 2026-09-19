"""Notepad 的审批、作用域过滤、幂等和 Markdown 持久化测试。"""

import pytest

from tikiagent.context.memory.models import NotepadEntry
from tikiagent.context.memory.notepad import (
    InMemoryNotepadStore,
    MarkdownNotepadStore,
    NotepadConflictError,
)


def note(note_id: str, *, scope: str, approved: bool = True, **kwargs):
    return NotepadEntry.model_validate(
        {
            "note_id": note_id,
            "content": f"fact:{note_id}",
            "scope": scope,
            "source_refs": [f"source:{note_id}"],
            "approved": approved,
            **kwargs,
        }
    )


def test_notepad_requires_approval_and_filters_scope() -> None:
    store = InMemoryNotepadStore()
    with pytest.raises(ValueError, match="未批准"):
        store.append(note("candidate", scope="global", approved=False))

    store.append(note("global", scope="global"))
    store.append(note("session", scope="session", session_id="session-1"))
    store.append(note("task", scope="task", task_id="task-1"))
    store.append(
        note(
            "research-only",
            scope="agent",
            agent_scope={"research_agent"},
        )
    )

    code_notes = store.list_relevant(
        task_id="task-1",
        session_id="session-1",
        agent="code_agent",
        limit=10,
    )
    assert [item.note_id for item in code_notes] == ["global", "session", "task"]


def test_notepad_write_is_idempotent_and_conflicts_are_rejected() -> None:
    store = InMemoryNotepadStore()
    entry = note("same", scope="global")

    assert store.append(entry) == store.append(entry)
    with pytest.raises(NotepadConflictError):
        store.append(entry.model_copy(update={"content": "different"}))


def test_markdown_notepad_can_reload_structured_entries(tmp_path) -> None:
    path = tmp_path / ".tiki" / "NOTEPAD.md"
    store = MarkdownNotepadStore(path)
    store.append(note("durable", scope="task", task_id="task-1"))

    reloaded = MarkdownNotepadStore(path)

    assert reloaded.get_by_id("durable") is not None
    assert "fact:durable" in path.read_text(encoding="utf-8")
