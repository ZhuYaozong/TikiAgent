"""Base Context 与 Local Memory 的确定性压缩器。"""

from dataclasses import dataclass
from typing import Protocol

from tikiagent.context.memory.models import LocalMemory, NotepadEntry
from tikiagent.context.models import BaseContext


@dataclass(frozen=True, slots=True)
class BaseCompressionResult:
    context: BaseContext
    changed: bool
    notepad_candidates: tuple[NotepadEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class LocalCompressionResult:
    memory: LocalMemory
    changed: bool


class BaseCompressor(Protocol):
    def compress(self, context: BaseContext) -> BaseCompressionResult: ...


class LocalCompressor(Protocol):
    def compress(
        self,
        memory: LocalMemory,
        *,
        recent_interaction_limit: int,
    ) -> LocalCompressionResult: ...


class RuleBasedBaseCompressor:
    """保留身份引用，以摘要真正替换其他旧 History。"""

    def compress(self, context: BaseContext) -> BaseCompressionResult:
        memory = context.working_memory
        protected = set(memory.protected_refs)
        preserved = [
            item
            for item in memory.relevant_history
            if item.record_id in protected
        ]
        removed = [
            item
            for item in memory.relevant_history
            if item.record_id not in protected
        ]
        if not removed:
            return BaseCompressionResult(context=context, changed=False)

        type_counts: dict[str, int] = {}
        for item in removed:
            type_counts[item.record_type] = type_counts.get(item.record_type, 0) + 1
        recent_facts = "；".join(
            f"{item.record_type}:{item.summary[:240]}" for item in removed[-4:]
        )
        new_summary = (
            f"已压缩 {len(removed)} 条非显式引用 History，"
            f"类型={type_counts}；{recent_facts}"
        )[-2000:]
        if memory.history_summary:
            new_summary = (
                f"{memory.history_summary}；{new_summary}"
            )[-2000:]

        candidates: list[NotepadEntry] = []
        for item in removed:
            durable_fact = item.payload.get("durable_fact")
            if isinstance(durable_fact, str) and durable_fact.strip():
                candidates.append(
                    NotepadEntry(
                        note_id=f"candidate:{item.record_id}",
                        content=durable_fact.strip(),
                        scope="task",
                        task_id=item.task_id,
                        session_id=item.session_id,
                        source_refs=[item.record_id],
                        approved=False,
                    )
                )

        compressed_memory = memory.model_copy(
            update={
                "history_summary": new_summary,
                # old History 已被 summary 替换，不在后面重复追加。
                "relevant_history": preserved,
            }
        )
        return BaseCompressionResult(
            context=context.model_copy(
                update={"working_memory": compressed_memory}
            ),
            changed=True,
            notepad_candidates=tuple(candidates),
        )


class RuleBasedLocalCompressor:
    """只按完整 ReAct Interaction 裁剪，绝不拆 ToolCall/Result。"""

    def compress(
        self,
        memory: LocalMemory,
        *,
        recent_interaction_limit: int,
    ) -> LocalCompressionResult:
        interactions = memory.recent_interactions
        if len(interactions) <= recent_interaction_limit:
            return LocalCompressionResult(memory=memory, changed=False)

        removed = interactions[:-recent_interaction_limit]
        retained = interactions[-recent_interaction_limit:]
        facts: list[str] = []
        for interaction in removed:
            raw_calls = interaction.assistant_message.get("tool_calls", [])
            tool_names = [
                str(item.get("function", {}).get("name", "unknown"))
                for item in raw_calls
                if isinstance(item, dict)
            ]
            statuses = []
            for message in interaction.tool_messages:
                content = str(message.get("content", ""))
                statuses.append("failed" if '"ok":false' in content else "observed")
            facts.append(
                f"{interaction.interaction_id}:"
                f"{','.join(tool_names)}:{','.join(statuses)}"
            )
        summary = "旧 ReAct 摘要：" + "；".join(facts)
        if memory.summary:
            summary = f"{memory.summary}；{summary}"
        compressed = LocalMemory(
            summary=summary[-2000:],
            recent_interactions=retained,
        )
        return LocalCompressionResult(memory=compressed, changed=True)
