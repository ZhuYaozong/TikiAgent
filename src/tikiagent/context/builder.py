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
from tikiagent.context.profiles import DEFAULT_CONTEXT_PROFILES
from tikiagent.context.retriever import Retriever
from tikiagent.context.task_board import todos_for_owner, todos_for_refs


class ContextBuilder:
    """组合运行状态、Task Board 和相关 History，不保存内部 messages。"""

    def __init__(
        self,
        retriever: Retriever,
        profiles: Mapping[ContextAgentName, ContextProfile] | None = None,
    ) -> None:
        self.retriever = retriever
        self.profiles = {
            **DEFAULT_CONTEXT_PROFILES,
            **dict(profiles or {}),
        }

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

        return BaseContext(
            agent=request.agent,
            role=profile.role,
            system_rules=profile.system_rules,
            working_memory=WorkingMemory(
                task=task,
                phase=request.phase,
                instruction=request.instruction,
                acceptance_criteria=acceptance_criteria,
                todos=todos,
                relevant_history=history,
            ),
        )
