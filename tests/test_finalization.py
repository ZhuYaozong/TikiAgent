"""Finalization 的 Guard 后执行、证据保留和幂等测试。"""

import pytest

from tikiagent.context import (
    FinalizationService,
    InMemoryHistoryStore,
    InMemoryNotepadStore,
    NotepadEntry,
)
from tikiagent.context.history import HistoryConflictError


def service():
    history = InMemoryHistoryStore()
    notepad = InMemoryNotepadStore()
    return FinalizationService(history_store=history, notepad_store=notepad), history, notepad


def test_finalization_is_idempotent_and_only_clears_explicit_runtime() -> None:
    finalizer, history, notepad = service()
    runtime = {
        "local_summary": "temporary",
        "recent_interactions": ["temporary"],
        "candidate_context_cache": {"temporary": True},
    }
    note = NotepadEntry(
        note_id="approved-note",
        content="长期约束",
        scope="task",
        task_id="task-1",
        source_refs=["result-1"],
        approved=True,
    )

    first = finalizer.finalize(
        task_id="task-1",
        session_id="session-1",
        final_result_id="final:task-1",
        final_result="任务完成",
        refs=["result-1", "verification-1"],
        approved_notepad=[note],
        ephemeral_runtime=runtime,
    )
    second = finalizer.finalize(
        task_id="task-1",
        session_id="session-1",
        final_result_id="final:task-1",
        final_result="任务完成",
        refs=["result-1", "verification-1"],
        approved_notepad=[note],
    )

    assert first.already_finalized is False
    assert second.already_finalized is True
    assert runtime == {}
    assert first.cleared_runtime_fields == [
        "local_summary",
        "recent_interactions",
        "candidate_context_cache",
    ]
    assert len(history.list_records()) == 1
    assert history.get_by_id("result-1") is None
    assert notepad.get_by_id("approved-note") == note


def test_same_final_result_id_cannot_change_final_fact() -> None:
    finalizer, _, _ = service()
    finalizer.finalize(
        task_id="task-1",
        session_id="session-1",
        final_result_id="final:task-1",
        final_result="任务完成",
        refs=["result-1"],
    )

    with pytest.raises(HistoryConflictError, match="final_result_id 冲突"):
        finalizer.finalize(
            task_id="task-1",
            session_id="session-1",
            final_result_id="final:task-1",
            final_result="被篡改的结果",
            refs=["result-1"],
        )
