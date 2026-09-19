"""把 Prompt、Base、Local 和 Schema 组装成候选模型调用。"""

from tikiagent.context.memory.models import LocalMemory
from tikiagent.context.models import (
    BaseContext,
    CandidateModelCall,
    PromptBundle,
    ToolView,
)


class CallContextAssembler:
    """每次调用都从结构化部件重新组装，避免旧消息隐式泄漏。"""

    def assemble(
        self,
        *,
        prompt: PromptBundle,
        base_context: BaseContext,
        local_memory: LocalMemory,
        tool_view: ToolView,
        response_schema: dict | None = None,
    ) -> CandidateModelCall:
        system_content = prompt.render()
        if local_memory.summary:
            system_content += (
                "\n本次 Agent.run() 的旧局部交互摘要："
                f"{local_memory.summary}"
            )
        messages: list[dict] = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": base_context.render()},
        ]
        for interaction in local_memory.recent_interactions:
            messages.append(interaction.assistant_message)
            messages.extend(interaction.tool_messages)
        return CandidateModelCall(
            prompt=prompt,
            base_context=base_context,
            local_memory=local_memory,
            tool_view=tool_view,
            response_schema=response_schema,
            messages=messages,
        )
