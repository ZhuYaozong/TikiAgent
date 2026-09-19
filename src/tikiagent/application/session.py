"""Session 与用户 Turn 的本地持久化。"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import TypeAlias
from uuid import uuid4
import os
import re

from pydantic import TypeAdapter, ValidationError

from tikiagent.application.models import (
    IntentDecision,
    ResponseRecord,
    SessionRecord,
    TurnRecord,
    utc_now,
)


TurnStoreRecord: TypeAlias = TurnRecord | ResponseRecord
_TURN_RECORD_ADAPTER = TypeAdapter(TurnStoreRecord)


class SessionError(RuntimeError):
    """Session/Turn Store 基础异常。"""


class SessionNotFoundError(SessionError):
    pass


class SessionConflictError(SessionError):
    pass


class SessionIntegrityError(SessionError):
    pass


class JsonSessionStore:
    """每个 Session 一个原子 JSON，并通过 revision 执行 CAS。"""

    _SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def create(self, *, workspace_id: str) -> SessionRecord:
        session = SessionRecord(workspace_id=workspace_id)
        self.save(session)
        return session

    def load(self, session_id: str) -> SessionRecord:
        path = self._path(session_id)
        if not path.exists():
            raise SessionNotFoundError(f"Session 不存在：{session_id}")
        try:
            return SessionRecord.model_validate_json(
                path.read_text(encoding="utf-8"),
                strict=False,
            )
        except (OSError, ValidationError) as error:
            raise SessionIntegrityError("Session JSON 或 Schema 不合法") from error

    def save(
        self,
        session: SessionRecord,
        *,
        expected_revision: int | None = None,
    ) -> SessionRecord:
        path = self._path(session.session_id)
        with self._lock:
            if path.exists():
                current = self.load(session.session_id)
                if expected_revision is None:
                    raise SessionConflictError("更新 Session 必须提供 expected_revision")
                if current.revision != expected_revision:
                    raise SessionConflictError(
                        "Session revision 冲突："
                        f"expected={expected_revision}, actual={current.revision}"
                    )
                if session.revision != expected_revision + 1:
                    raise SessionConflictError("Session 新 revision 必须恰好增加 1")
            elif expected_revision is not None or session.revision != 1:
                raise SessionConflictError("新 Session 必须从 revision=1 创建")

            temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(session.model_dump_json(indent=2))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return session

    def _path(self, session_id: str) -> Path:
        if not self._SAFE_ID.fullmatch(session_id):
            raise SessionIntegrityError("Session ID 包含不安全路径字符")
        return self.root / f"{session_id}.json"


class JsonlTurnStore:
    """用户 Turn 和应用响应独立保存，不写入 Session JSON。"""

    _SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def append(self, record: TurnStoreRecord) -> TurnStoreRecord:
        path = self._path(record.session_id)
        with self._lock, path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(record.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return record

    def list_records(self, session_id: str) -> list[TurnStoreRecord]:
        path = self._path(session_id)
        if not path.exists():
            return []
        records: list[TurnStoreRecord] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(_TURN_RECORD_ADAPTER.validate_json(line))
        except (OSError, ValidationError, ValueError) as error:
            raise SessionIntegrityError("Turn History JSONL 不合法") from error
        return records

    def recent_messages(
        self,
        session_id: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, str]]:
        if limit < 0:
            raise ValueError("limit 不能小于 0")
        messages: list[dict[str, str]] = []
        for record in self.list_records(session_id):
            if isinstance(record, TurnRecord):
                messages.append({"role": "user", "content": record.user_input})
            elif record.status in {"chat_completed", "workflow_completed"}:
                messages.append({"role": "assistant", "content": record.content})
        return messages[-limit:] if limit else []

    def _path(self, session_id: str) -> Path:
        if not self._SAFE_ID.fullmatch(session_id):
            raise SessionIntegrityError("Session ID 包含不安全路径字符")
        return self.root / f"{session_id}.jsonl"


class SessionService:
    """管理 Session 元数据，但不读取 Checkpoint 推断 Workflow 状态。"""

    def __init__(
        self,
        *,
        sessions: JsonSessionStore,
        turns: JsonlTurnStore,
    ) -> None:
        self.sessions = sessions
        self.turns = turns

    def create(self, *, workspace_id: str) -> SessionRecord:
        return self.sessions.create(workspace_id=workspace_id)

    def record_turn(
        self,
        *,
        session_id: str,
        user_input: str,
        decision: IntentDecision,
        task_id: str | None,
    ) -> tuple[SessionRecord, TurnRecord]:
        session = self.sessions.load(session_id)
        turn = TurnRecord(
            session_id=session_id,
            task_id=task_id,
            user_input=user_input,
            decision=decision,
        )
        self.turns.append(turn)
        updated = session.model_copy(
            update={
                "revision": session.revision + 1,
                "turn_count": session.turn_count + 1,
                "last_task_id": task_id or session.last_task_id,
                "updated_at": utc_now(),
            }
        )
        self.sessions.save(updated, expected_revision=session.revision)
        return updated, turn

    def bind_checkpoint(
        self,
        *,
        session_id: str,
        checkpoint_id: str,
    ) -> SessionRecord:
        """调用方必须先确认 Checkpoint 已成功持久化。"""

        session = self.sessions.load(session_id)
        updated = session.model_copy(
            update={
                "revision": session.revision + 1,
                "active_checkpoint_id": checkpoint_id,
                "updated_at": utc_now(),
            }
        )
        return self.sessions.save(updated, expected_revision=session.revision)

    def clear_checkpoint(self, *, session_id: str) -> SessionRecord:
        session = self.sessions.load(session_id)
        updated = session.model_copy(
            update={
                "revision": session.revision + 1,
                "active_checkpoint_id": None,
                "updated_at": utc_now(),
            }
        )
        return self.sessions.save(updated, expected_revision=session.revision)

    def record_response(self, response: ResponseRecord) -> ResponseRecord:
        self.turns.append(response)
        return response
