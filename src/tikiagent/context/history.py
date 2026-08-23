"""History Store 接口与 v0.5 内存实现。"""

from __future__ import annotations

from collections.abc import Iterable
from threading import RLock
from typing import Protocol

from tikiagent.context.models import HistoryRecord, HistoryRecordType


class HistoryConflictError(RuntimeError):
    """同一个实体 ID 被写入不同内容。"""


class HistoryStore(Protocol):
    """History 后端协议，后续可以替换为持久化实现。"""

    def append(self, record: HistoryRecord) -> HistoryRecord: ...

    def get_by_id(self, record_id: str) -> HistoryRecord | None: ...

    def search_keyword(
        self,
        *,
        task_id: str,
        session_id: str,
        keywords: list[str],
        record_types: set[HistoryRecordType],
        limit: int,
    ) -> list[HistoryRecord]: ...

    def recent(
        self,
        *,
        task_id: str,
        session_id: str,
        record_types: set[HistoryRecordType],
        limit: int,
    ) -> list[HistoryRecord]: ...

    def list_records(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> list[HistoryRecord]: ...

    def cursor(self) -> int: ...


class InMemoryHistoryStore:
    """线程安全的进程内 History；持久化留到后续阶段。"""

    def __init__(self) -> None:
        self._records: dict[str, HistoryRecord] = {}
        self._order: list[str] = []
        self._sequence = 0
        self._lock = RLock()

    def append(self, record: HistoryRecord) -> HistoryRecord:
        """按实体 ID 幂等写入，冲突内容立即失败。"""

        with self._lock:
            existing = self._records.get(record.record_id)
            if existing is not None:
                normalized = record.model_copy(
                    update={"sequence": existing.sequence}
                )
                if normalized == existing:
                    return existing
                raise HistoryConflictError(
                    f"History record_id 冲突：{record.record_id}"
                )

            self._sequence += 1
            stored = record.model_copy(update={"sequence": self._sequence})
            self._records[stored.record_id] = stored
            self._order.append(stored.record_id)
            return stored

    def get_by_id(self, record_id: str) -> HistoryRecord | None:
        with self._lock:
            return self._records.get(record_id)

    def search_keyword(
        self,
        *,
        task_id: str,
        session_id: str,
        keywords: list[str],
        record_types: set[HistoryRecordType],
        limit: int,
    ) -> list[HistoryRecord]:
        normalized = [item.casefold() for item in keywords if item.strip()]
        if not normalized or limit == 0:
            return []
        matches: list[HistoryRecord] = []
        for record in self._iter_scope(task_id, session_id):
            if record.record_type not in record_types:
                continue
            searchable = (
                f"{record.summary} {' '.join(record.refs)}"
            ).casefold()
            if any(keyword in searchable for keyword in normalized):
                matches.append(record)
        return matches[-limit:]

    def recent(
        self,
        *,
        task_id: str,
        session_id: str,
        record_types: set[HistoryRecordType],
        limit: int,
    ) -> list[HistoryRecord]:
        if limit == 0:
            return []
        records = [
            record
            for record in self._iter_scope(task_id, session_id)
            if record.record_type in record_types
        ]
        return records[-limit:]

    def list_records(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> list[HistoryRecord]:
        with self._lock:
            records = [self._records[item] for item in self._order]
        return [
            record
            for record in records
            if (task_id is None or record.task_id == task_id)
            and (session_id is None or record.session_id == session_id)
        ]

    def cursor(self) -> int:
        with self._lock:
            return self._sequence

    def _iter_scope(
        self,
        task_id: str,
        session_id: str,
    ) -> Iterable[HistoryRecord]:
        return self.list_records(task_id=task_id, session_id=session_id)
