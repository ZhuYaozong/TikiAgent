"""带作用域过滤和幂等写入的长期 Notepad Context Source。"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Protocol

from tikiagent.context.memory.models import NotepadEntry
from tikiagent.context.schema import ContextAgentName


class NotepadConflictError(RuntimeError):
    """同一个 note_id 被写入了不同事实。"""


class NotepadStore(Protocol):
    def append(self, entry: NotepadEntry) -> NotepadEntry: ...

    def get_by_id(self, note_id: str) -> NotepadEntry | None: ...

    def list_relevant(
        self,
        *,
        task_id: str,
        session_id: str,
        agent: ContextAgentName,
        limit: int,
    ) -> list[NotepadEntry]: ...


class InMemoryNotepadStore:
    """默认进程内实现；接口可替换为 Workspace Markdown 实现。"""

    def __init__(self) -> None:
        self._entries: dict[str, NotepadEntry] = {}
        self._order: list[str] = []
        self._lock = RLock()

    def append(self, entry: NotepadEntry) -> NotepadEntry:
        if not entry.approved:
            raise ValueError("未批准的 Notepad 候选不能写入")
        with self._lock:
            existing = self._entries.get(entry.note_id)
            if existing is not None:
                if existing == entry:
                    return existing
                raise NotepadConflictError(
                    f"Notepad note_id 冲突：{entry.note_id}"
                )
            self._entries[entry.note_id] = entry
            self._order.append(entry.note_id)
            return entry

    def get_by_id(self, note_id: str) -> NotepadEntry | None:
        with self._lock:
            return self._entries.get(note_id)

    def list_relevant(
        self,
        *,
        task_id: str,
        session_id: str,
        agent: ContextAgentName,
        limit: int,
    ) -> list[NotepadEntry]:
        if limit == 0:
            return []
        with self._lock:
            entries = [self._entries[item] for item in self._order]
        relevant = [
            item
            for item in entries
            if _matches_scope(
                item,
                task_id=task_id,
                session_id=session_id,
                agent=agent,
            )
        ]
        return relevant[-limit:]


class MarkdownNotepadStore(InMemoryNotepadStore):
    """把结构化条目持久化到可读的 `.tiki/NOTEPAD.md`。"""

    START = "<!-- TIKIAGENT_NOTES_START -->"
    END = "<!-- TIKIAGENT_NOTES_END -->"

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path.resolve()
        self._load()

    def append(self, entry: NotepadEntry) -> NotepadEntry:
        stored = super().append(entry)
        self._persist()
        return stored

    def _load(self) -> None:
        if not self.path.exists():
            return
        text = self.path.read_text(encoding="utf-8")
        if self.START not in text or self.END not in text:
            return
        payload = text.split(self.START, 1)[1].split(self.END, 1)[0]
        for line in payload.splitlines():
            if not line.strip():
                continue
            entry = NotepadEntry.model_validate_json(line)
            # 文件中的条目已在以前经过批准。
            super().append(entry)

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entries = [self._entries[item] for item in self._order]
        machine_lines = "\n".join(item.model_dump_json() for item in entries)
        readable = "\n\n".join(
            f"## {item.note_id}\n\n{item.content}\n\n"
            f"Sources: {', '.join(item.source_refs)}"
            for item in entries
        )
        text = (
            "# TikiAgent Notepad\n\n"
            f"{self.START}\n{machine_lines}\n{self.END}\n\n"
            f"{readable}\n"
        )
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.path)


def _matches_scope(
    entry: NotepadEntry,
    *,
    task_id: str,
    session_id: str,
    agent: ContextAgentName,
) -> bool:
    if not entry.approved:
        return False
    if entry.scope == "global":
        return True
    if entry.scope == "session":
        return entry.session_id == session_id
    if entry.scope == "task":
        return entry.task_id == task_id
    return agent in entry.agent_scope and (
        entry.task_id is None or entry.task_id == task_id
    ) and (entry.session_id is None or entry.session_id == session_id)
