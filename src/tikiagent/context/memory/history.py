"""History Store 接口与 v0.5 内存实现。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from threading import RLock
from typing import Protocol
import os

from tikiagent.context.memory.models import HistoryRecord
from tikiagent.context.schema import HistoryRecordType


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


class JsonlHistoryStore:
    """可跨进程重建的 JSONL History；Trace 不参与重建。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory = InMemoryHistoryStore()
        self._lock = RLock()
        self._load_existing()

    def append(self, record: HistoryRecord) -> HistoryRecord:
        """先持久化再发布到内存视图，保持进程内外一致。"""

        with self._lock:
            existing = self._memory.get_by_id(record.record_id)
            if existing is not None:
                normalized = record.model_copy(update={"sequence": existing.sequence})
                if normalized == existing:
                    return existing
                raise HistoryConflictError(
                    f"History record_id 冲突：{record.record_id}"
                )
            stored = record.model_copy(
                update={"sequence": self._memory.cursor() + 1}
            )
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(stored.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return self._memory.append(stored)

    def get_by_id(self, record_id: str) -> HistoryRecord | None:
        return self._memory.get_by_id(record_id)

    def search_keyword(
        self,
        *,
        task_id: str,
        session_id: str,
        keywords: list[str],
        record_types: set[HistoryRecordType],
        limit: int,
    ) -> list[HistoryRecord]:
        return self._memory.search_keyword(
            task_id=task_id,
            session_id=session_id,
            keywords=keywords,
            record_types=record_types,
            limit=limit,
        )

    def recent(
        self,
        *,
        task_id: str,
        session_id: str,
        record_types: set[HistoryRecordType],
        limit: int,
    ) -> list[HistoryRecord]:
        return self._memory.recent(
            task_id=task_id,
            session_id=session_id,
            record_types=record_types,
            limit=limit,
        )

    def list_records(
        self,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> list[HistoryRecord]:
        return self._memory.list_records(
            task_id=task_id,
            session_id=session_id,
        )

    def cursor(self) -> int:
        return self._memory.cursor()

    def require_cursor(self, minimum_cursor: int) -> None:
        """恢复时确认 History 至少包含 Checkpoint 已观察到的记录。"""

        if self.cursor() < minimum_cursor:
            raise HistoryConflictError(
                f"History cursor 落后：required={minimum_cursor}, "
                f"actual={self.cursor()}"
            )

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        expected_sequence = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            expected_sequence += 1
            record = HistoryRecord.model_validate_json(line)
            if record.sequence != expected_sequence:
                raise HistoryConflictError(
                    "History JSONL sequence 不连续，拒绝猜测恢复状态"
                )
            self._memory.append(record)
