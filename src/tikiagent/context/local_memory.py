"""单次 Agent.run() 内的原子 ReAct Interaction 管理。"""

from tikiagent.context.models import LocalMemory, ReActInteraction


class LocalMemoryManager:
    """只在一次 Agent 执行内存活，重新委派必须创建新实例。"""

    def __init__(self, memory: LocalMemory | None = None) -> None:
        self._memory = memory or LocalMemory()

    @property
    def memory(self) -> LocalMemory:
        return self._memory

    def replace(self, memory: LocalMemory) -> None:
        self._memory = memory

    def append(
        self,
        *,
        interaction_id: str,
        assistant_message: dict,
        tool_messages: list[dict],
    ) -> ReActInteraction:
        interaction = ReActInteraction(
            interaction_id=interaction_id,
            assistant_message=assistant_message,
            tool_messages=tool_messages,
        )
        self._memory = self._memory.model_copy(
            update={
                "recent_interactions": [
                    *self._memory.recent_interactions,
                    interaction,
                ]
            }
        )
        return interaction

    def clear(self) -> None:
        self._memory = LocalMemory()
