"""协调 Prompt、工具视图、监控与压缩，准备模型输入。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeVar

from pydantic import BaseModel

from tikiagent.context.call_context import CallContextAssembler
from tikiagent.context.compression.compressors import (
    BaseCompressor,
    LocalCompressor,
    RuleBasedBaseCompressor,
    RuleBasedLocalCompressor,
)
from tikiagent.context.compression.models import ContextBudget, ContextUsage
from tikiagent.context.compression.monitor import ContextMonitor
from tikiagent.context.compression.policy import CompressionPolicy
from tikiagent.context.memory.models import LocalMemory
from tikiagent.context.models import (
    BaseContext,
    CandidateModelCall,
    ContextProfile,
    PreparedModelCall,
)
from tikiagent.context.prompt import PromptAssembler
from tikiagent.context.schema import ContextAgentName
from tikiagent.context.tool_selection import ToolSelector
from tikiagent.tools.registry import ToolRegistry


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
        observer=None,
        budget_resolver=None,
    ) -> None:
        self.budget = budget or ContextBudget()
        self.prompt_assembler = PromptAssembler(profiles)
        self.tool_selector = ToolSelector(profiles)
        self.call_assembler = CallContextAssembler()
        self.monitor = monitor or ContextMonitor()
        self.base_compressor = base_compressor or RuleBasedBaseCompressor()
        self.local_compressor = local_compressor or RuleBasedLocalCompressor()
        self.compression_policy = compression_policy or CompressionPolicy()
        self.observer = observer
        self.budget_resolver = budget_resolver

    def prepare(
        self,
        *,
        base_context: BaseContext,
        local_memory: LocalMemory,
        registry: ToolRegistry,
        response_type: type[ResponseModel] | None = None,
    ) -> PreparedModelCall:
        phase = base_context.working_memory.phase
        # 同一阶段的 Context 与 API 必须采用同一输出预留量。
        if self.budget_resolver:
            self.budget = self.budget_resolver(base_context.agent, phase)
        engines = {id(e): e for e in (getattr(self.base_compressor, "engine", None), getattr(self.local_compressor, "engine", None)) if e is not None}
        for engine in engines.values():
            engine.calls = max(engine.calls, base_context.working_memory.compression_calls)
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
        before_usage = usage

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
            # 按完整交互缩小近期窗口；至少留下最近一组，过大则明确报错。
            interactions = candidate.local_memory.recent_interactions
            keep = min(self.budget.recent_interaction_limit, len(interactions))
            while keep > 1 and self.monitor.estimator.estimate([
                item.model_dump(mode="json") for item in interactions[-keep:]
            ]) > self.budget.recent_tokens_budget:
                keep -= 1
            result = self.local_compressor.compress(
                candidate.local_memory,
                recent_interaction_limit=max(1, keep),
                task=(candidate.base_context.working_memory.task + "\n"
                      + candidate.base_context.working_memory.instruction),
                scope="/".join([
                    candidate.base_context.working_memory.session_id,
                    candidate.base_context.working_memory.task_id,
                    candidate.base_context.agent,
                    *candidate.base_context.working_memory.protected_refs,
                ]),
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

        if engines:
            updated_memory = candidate.base_context.working_memory.model_copy(update={"compression_calls": max(e.calls for e in engines.values())})
            candidate = candidate.model_copy(update={"base_context": candidate.base_context.model_copy(update={"working_memory": updated_memory})})
        if self.observer:
            details = {"agent": base_context.agent, "before": before_usage.model_dump(),
                       "after": usage.model_dump(), "actions": actions}
            events = []
            seen = set()
            for compressor in (self.base_compressor, self.local_compressor):
                engine = getattr(compressor, "engine", None)
                if engine is not None and id(engine) not in seen:
                    seen.add(id(engine))
                    events.extend(engine.events)
                    engine.events.clear()
            details["compression_events"] = events
            self.observer(base_context, details)
        if usage.total_over_budget:
            raise ContextBudgetExceeded(candidate, usage)

        return PreparedModelCall(
            **candidate.model_dump(mode="python"),
            usage=usage,
            compression_actions=actions,
            notepad_candidates=list(notepad_candidates),
        )
