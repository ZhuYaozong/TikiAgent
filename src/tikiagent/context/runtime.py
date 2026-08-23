"""Candidate → Monitor → Compression → Prepared 的调用运行时。"""

from collections.abc import Mapping
from typing import Literal, TypeVar

from pydantic import BaseModel

from tikiagent.context.call_context import CallContextAssembler
from tikiagent.context.compressor import (
    BaseCompressor,
    LocalCompressor,
    RuleBasedBaseCompressor,
    RuleBasedLocalCompressor,
)
from tikiagent.context.models import (
    BaseContext,
    CandidateModelCall,
    ContextAgentName,
    ContextBudget,
    ContextProfile,
    ContextUsage,
    LocalMemory,
    PreparedModelCall,
)
from tikiagent.context.monitor import ContextMonitor
from tikiagent.context.prompt import PromptAssembler
from tikiagent.context.tool_view import ToolSelector
from tikiagent.harness.registry import ToolRegistry


ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class ContextBudgetExceeded(RuntimeError):
    """压缩后仍无法为输入和输出保留安全窗口。"""

    def __init__(
        self,
        candidate: CandidateModelCall,
        usage: ContextUsage,
    ) -> None:
        self.candidate = candidate
        self.usage = usage
        super().__init__(f"模型调用上下文仍超过预算：{usage}")


class CompressionPolicy:
    """根据分项和总预算决定哪类 Context 需要尝试压缩。"""

    @staticmethod
    def should_compress_base(usage: ContextUsage) -> bool:
        return usage.base_over_budget or usage.total_over_budget

    @staticmethod
    def should_compress_local(usage: ContextUsage) -> bool:
        return usage.local_over_budget or usage.total_over_budget


class ContextRuntime:
    """为每一轮模型调用生成可发送的 PreparedModelCall。"""

    def __init__(
        self,
        *,
        budget: ContextBudget | None = None,
        profiles: Mapping[ContextAgentName, ContextProfile] | None = None,
        monitor: ContextMonitor | None = None,
        base_compressor: BaseCompressor | None = None,
        local_compressor: LocalCompressor | None = None,
        compression_policy: CompressionPolicy | None = None,
    ) -> None:
        self.budget = budget or ContextBudget()
        self.prompt_assembler = PromptAssembler(profiles)
        self.tool_selector = ToolSelector(profiles)
        self.call_assembler = CallContextAssembler()
        self.monitor = monitor or ContextMonitor()
        self.base_compressor = base_compressor or RuleBasedBaseCompressor()
        self.local_compressor = local_compressor or RuleBasedLocalCompressor()
        self.compression_policy = compression_policy or CompressionPolicy()

    def prepare(
        self,
        *,
        base_context: BaseContext,
        local_memory: LocalMemory,
        registry: ToolRegistry,
        response_type: type[ResponseModel] | None = None,
    ) -> PreparedModelCall:
        phase = base_context.working_memory.phase
        prompt = self.prompt_assembler.assemble(base_context.agent, phase)
        tool_view = self.tool_selector.select(
            agent=base_context.agent,
            phase=phase,
            registry=registry,
        )
        response_schema = (
            response_type.model_json_schema() if response_type is not None else None
        )
        candidate = self.call_assembler.assemble(
            prompt=prompt,
            base_context=base_context,
            local_memory=local_memory,
            tool_view=tool_view,
            response_schema=response_schema,
        )
        actions: list[Literal["base", "local"]] = []
        notepad_candidates = []
        usage = self.monitor.measure(candidate, self.budget)

        if self.compression_policy.should_compress_base(usage):
            result = self.base_compressor.compress(candidate.base_context)
            if result.changed:
                actions.append("base")
                notepad_candidates.extend(result.notepad_candidates)
                candidate = self.call_assembler.assemble(
                    prompt=prompt,
                    base_context=result.context,
                    local_memory=candidate.local_memory,
                    tool_view=tool_view,
                    response_schema=response_schema,
                )
                usage = self.monitor.measure(candidate, self.budget)

        if self.compression_policy.should_compress_local(usage):
            result = self.local_compressor.compress(
                candidate.local_memory,
                recent_interaction_limit=self.budget.recent_interaction_limit,
            )
            if result.changed:
                actions.append("local")
                candidate = self.call_assembler.assemble(
                    prompt=prompt,
                    base_context=candidate.base_context,
                    local_memory=result.memory,
                    tool_view=tool_view,
                    response_schema=response_schema,
                )
                usage = self.monitor.measure(candidate, self.budget)

        if usage.total_over_budget:
            raise ContextBudgetExceeded(candidate, usage)

        return PreparedModelCall(
            **candidate.model_dump(mode="python"),
            usage=usage,
            compression_actions=actions,
            notepad_candidates=list(notepad_candidates),
        )
