"""按 Agent Profile 构建 Base Context。"""

from collections.abc import Mapping

from tikiagent.context.models import (
    BaseContext,
    ContextAgentName,
    ContextProfile,
    ContextRequest,
    TaskBoard,
    WorkingMemory,
)
from tikiagent.context.notepad import InMemoryNotepadStore, NotepadStore
from tikiagent.context.profiles import DEFAULT_CONTEXT_PROFILES
from tikiagent.context.retriever import Retriever
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
        history = self.retriever.retrieve(request, profile)

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
                phase=request.phase,
                instruction=request.instruction,
                acceptance_criteria=acceptance_criteria,
                todos=todos,
                relevant_history=history,
                relevant_notepad=relevant_notepad,
                protected_refs=protected_refs,
            ),
        )
