"""按 Agent Profile 构建 Base Context。"""

from collections.abc import Mapping

from tikiagent.context.memory.notepad import InMemoryNotepadStore, NotepadStore
from tikiagent.context.memory.retriever import Retriever
from tikiagent.context.models import (
    BaseContext,
    ContextProfile,
    ContextRequest,
    TaskBoard,
    WorkingMemory,
)
from tikiagent.context.profiles import DEFAULT_CONTEXT_PROFILES
from tikiagent.context.schema import ContextAgentName
from tikiagent.context.task_board import todos_for_owner, todos_for_refs


class ContextBuilder:
    """组合运行状态、Task Board 和相关 History，不保存内部 messages。"""

    def __init__(
        self,
        retriever: Retriever,
        profiles: Mapping[ContextAgentName, ContextProfile] | None = None,
        notepad_store: NotepadStore | None = None,
    ) -> None:
        self.retriever = retriever
        self.profiles = {
            **DEFAULT_CONTEXT_PROFILES,
            **dict(profiles or {}),
        }
        self.notepad_store = notepad_store or InMemoryNotepadStore()

    def build(
        self,
        *,
        request: ContextRequest,
        task: str,
        acceptance_criteria: list[str],
        task_board: TaskBoard,
    ) -> BaseContext:
        profile = self.profiles[request.agent]
        history = [self._history_view(record) for record in self.retriever.retrieve(request, profile)]

        if profile.include_global_task_board:
            todos = list(task_board.items.values())
        elif request.agent == "verifier":
            todos = todos_for_refs(task_board, request.context_refs)
        else:
            todos = todos_for_owner(task_board, request.agent)

        relevant_notepad = self.notepad_store.list_relevant(
            task_id=request.task_id,
            session_id=request.session_id,
            agent=request.agent,
            limit=profile.max_notepad_entries,
        )
        protected_refs = list(
            dict.fromkeys(
                [
                    *request.context_refs,
                    *[
                        value
                        for todo in todos
                        for value in (
                            todo.handoff_id,
                            todo.result_id,
                            todo.verification_id,
                        )
                        if value is not None
                    ],
                ]
            )
        )

        return BaseContext(
            agent=request.agent,
            working_memory=WorkingMemory(
                task=task,
                task_id=request.task_id,
                session_id=request.session_id,
                phase=request.phase,
                instruction=request.instruction,
                acceptance_criteria=acceptance_criteria,
                todos=todos,
                relevant_history=history,
                relevant_notepad=relevant_notepad,
                protected_refs=protected_refs,
            ),
        )

    @staticmethod
    def _history_view(record):
        """长 Result 正文只展示摘录；身份与来源保留，原文仍在 History Store。"""

        if record.record_type != "result":
            return record
        from copy import deepcopy

        payload = deepcopy(record.payload)
        changed = False
        if isinstance(payload.get("summary"), str) and len(payload["summary"]) > 2000:
            payload["summary"] = payload["summary"][:2000] + "…[结果摘要摘录]"
            changed = True
        for source in payload.get("sources", []):
            if isinstance(source, dict) and isinstance(source.get("snippet"), str) and len(source["snippet"]) > 500:
                source["snippet"] = source["snippet"][:500] + "…[来源摘录]"
                changed = True
        if not changed:
            return record
        payload["original_history_ref"] = record.record_id
        return record.model_copy(update={"summary": record.summary[:2000], "payload": payload})
