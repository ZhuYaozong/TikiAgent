"""History Store 跨进程恢复测试。"""

import pytest

from tikiagent.context import HistoryConflictError, HistoryRecord, JsonlHistoryStore


def record(record_id: str, summary: str) -> HistoryRecord:
    return HistoryRecord(
        record_id=record_id,
        task_id="task-1",
        session_id="session-1",
        record_type="result",
        producer="code_agent",
        summary=summary,
    )


def test_jsonl_history_rebuilds_records_and_cursor_in_new_process(tmp_path) -> None:
    path = tmp_path / "history.jsonl"
    first_runtime = JsonlHistoryStore(path)
    first_runtime.append(record("result-1", "first"))
    first_runtime.append(record("result-2", "second"))

    second_runtime = JsonlHistoryStore(path)

    assert second_runtime.cursor() == 2
    assert [item.record_id for item in second_runtime.list_records()] == [
        "result-1",
        "result-2",
    ]
    second_runtime.require_cursor(2)
    with pytest.raises(HistoryConflictError, match="落后"):
        second_runtime.require_cursor(3)


def test_jsonl_history_keeps_idempotent_record_identity(tmp_path) -> None:
    store = JsonlHistoryStore(tmp_path / "history.jsonl")
    stored = store.append(record("result-1", "first"))

    assert store.append(record("result-1", "first")) == stored
    with pytest.raises(HistoryConflictError, match="冲突"):
        store.append(record("result-1", "different"))
