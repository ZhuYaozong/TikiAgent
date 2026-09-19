"""History Store 的作用域、顺序与幂等测试。"""

import pytest

from tikiagent.context.memory.history import HistoryConflictError, InMemoryHistoryStore
from tikiagent.context.memory.models import HistoryRecord


def record(
    record_id: str,
    *,
    summary: str = "summary",
    task_id: str = "task-1",
) -> HistoryRecord:
    return HistoryRecord(
        record_id=record_id,
        task_id=task_id,
        session_id="session-1",
        record_type="result",
        producer="research_agent",
        summary=summary,
    )


def test_append_assigns_cursor_and_is_idempotent() -> None:
    store = InMemoryHistoryStore()

    first = store.append(record("result-1"))
    repeated = store.append(record("result-1"))

    assert first.sequence == 1
    assert repeated == first
    assert store.cursor() == 1
    assert len(store.list_records()) == 1


def test_same_id_with_different_content_is_rejected() -> None:
    store = InMemoryHistoryStore()
    store.append(record("result-1"))

    with pytest.raises(HistoryConflictError, match="result-1"):
        store.append(record("result-1", summary="different"))


def test_keyword_and_recent_respect_task_scope() -> None:
    store = InMemoryHistoryStore()
    store.append(record("task-1-result", summary="agent framework update"))
    store.append(
        record(
            "task-2-result",
            summary="agent framework unrelated task",
            task_id="task-2",
        )
    )

    matches = store.search_keyword(
        task_id="task-1",
        session_id="session-1",
        keywords=["framework"],
        record_types={"result"},
        limit=3,
    )
    recent = store.recent(
        task_id="task-1",
        session_id="session-1",
        record_types={"result"},
        limit=3,
    )

    assert [item.record_id for item in matches] == ["task-1-result"]
    assert [item.record_id for item in recent] == ["task-1-result"]
