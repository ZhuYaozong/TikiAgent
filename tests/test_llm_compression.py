"""两层 LLM 压缩的保真、预算、稳定前缀与失效处理。"""

import json
import pytest

from tikiagent.context.compression.llm import SummaryEngine, LLMBaseCompressor, LLMLocalCompressor
from tikiagent.context.compression.models import ContextBudget
from tikiagent.context.memory.models import LocalMemory
from tikiagent.context.preparation import ContextRuntime, ContextBudgetExceeded
from test_context_runtime import base_context, interaction, registry


class Summarizer:
    def __init__(self, *, bad_ref=False, fail=False):
        self.requests = []
        self.bad_ref = bad_ref
        self.fail = fail

    def complete_structured(self, *, messages, response_type):
        self.requests.append(messages)
        if self.fail:
            raise TimeoutError("离线模拟")
        data = json.loads(messages[1]["content"])
        return response_type(completed=["读取文件"], decisions=["继续修复"], unresolved=["需要验证"],
                             failures=["测试未通过"], next_steps=["修复后重新验证"],
                             source_refs=["invented"] if self.bad_ref else data["source_refs"][-2:])


def test_history_summary_is_cached_and_does_not_change_protected_facts():
    model = Summarizer()
    compressor = LLMBaseCompressor(SummaryEngine(model))
    original = base_context()
    first = compressor.compress(original)
    second = compressor.compress(original)
    assert first.changed and first.context == second.context
    assert len(model.requests) == 1
    memory = first.context.working_memory
    assert memory.task == original.working_memory.task
    assert memory.todos == original.working_memory.todos
    assert memory.acceptance_criteria == original.working_memory.acceptance_criteria
    assert memory.relevant_history[-1] == original.working_memory.relevant_history[-1]
    assert "history-0" in memory.history_summary_refs
    assert len(original.working_memory.relevant_history) == 6


def test_local_summary_keeps_exact_execution_facts_and_recent_pair():
    old = interaction(0, 3000)
    old.tool_messages[0]["content"] = json.dumps({"ok": True, "output": {"exit_code": 7, "path": "a.py", "stderr": "x" * 3000}})
    recent = interaction(1)
    memory = LocalMemory(recent_interactions=[old, recent])
    result = LLMLocalCompressor(SummaryEngine(Summarizer())).compress(memory, recent_interaction_limit=1, task="修复测试")
    assert result.changed
    assert result.memory.recent_interactions == [recent]
    assert result.memory.execution_facts[0]["exit_code"] == 7
    assert result.memory.execution_facts[0]["path"] == "a.py"
    assert result.memory.summary_refs == ["interaction-0"]
    assert len(memory.recent_interactions) == 2


def test_failed_or_fabricated_summary_keeps_original_memory():
    context = base_context()
    for model in [Summarizer(fail=True), Summarizer(bad_ref=True)]:
        engine = SummaryEngine(model)
        result = LLMBaseCompressor(engine).compress(context)
        assert not result.changed and result.context == context
        assert engine.events[-1]["kind"] == "compression_failed"


def test_summarizer_own_budget_prevents_recursive_or_unbounded_requests():
    model = Summarizer()
    engine = SummaryEngine(model, batch_chars=100, max_batches=2)
    assert engine.summarize(task="task", source="x" * 201, refs=[], scope="s") is None
    assert not model.requests


def test_stable_system_prefix_and_token_aware_recent_window():
    engine = SummaryEngine(Summarizer())
    runtime = ContextRuntime(budget=ContextBudget(local_messages_budget=200, recent_tokens_budget=100),
                             base_compressor=LLMBaseCompressor(engine), local_compressor=LLMLocalCompressor(engine))
    context = base_context()
    first = runtime.prepare(base_context=context, local_memory=LocalMemory(), registry=registry())
    second = runtime.prepare(base_context=context,
                             local_memory=LocalMemory(recent_interactions=[interaction(i, 2500) for i in range(6)]), registry=registry())
    assert first.messages[0] == second.messages[0]
    assert second.local_memory.summary
    assert len(second.local_memory.recent_interactions) == 1
    assert second.messages[-1]["tool_call_id"] == "call-5"
    assert second.local_memory.summary not in second.messages[0]["content"]


def test_repeated_failed_input_does_not_keep_calling_model():
    model = Summarizer(fail=True)
    engine = SummaryEngine(model)
    compressor = LLMBaseCompressor(engine)
    compressor.compress(base_context())
    compressor.compress(base_context())
    assert len(model.requests) == 1


def test_summary_budget_survives_restored_base_context():
    model = Summarizer()
    engine = SummaryEngine(model, max_calls=1)
    original = base_context()
    restored = original.model_copy(update={"working_memory": original.working_memory.model_copy(update={"compression_calls": 1})})
    runtime = ContextRuntime(budget=ContextBudget(base_context_budget=100), base_compressor=LLMBaseCompressor(engine),
                             local_compressor=LLMLocalCompressor(engine))
    prepared = runtime.prepare(base_context=restored, local_memory=LocalMemory(), registry=registry())
    assert not model.requests
    assert prepared.base_context.working_memory.compression_calls == 1


def test_single_huge_recent_group_is_not_split_or_silently_dropped():
    model = Summarizer()
    runtime = ContextRuntime(budget=ContextBudget(model_context_limit=3000, reserved_output_tokens=500),
                             local_compressor=LLMLocalCompressor(SummaryEngine(model)))
    memory = LocalMemory(recent_interactions=[interaction(0, 30_000)])
    with pytest.raises(ContextBudgetExceeded):
        runtime.prepare(base_context=base_context(), local_memory=memory, registry=registry())
    assert len(memory.recent_interactions[0].tool_messages) == 1
    assert not model.requests


def test_partial_batch_summary_never_replaces_complete_source():
    model = Summarizer()
    engine = SummaryEngine(model, batch_chars=1000, max_batches=1, input_budget=600)
    assert engine.summarize(task="任务", source="原始事实。" * 150, refs=["r"], scope="s") is None
    assert engine.calls <= 1


def test_schema_rendered_once_after_stable_rules():
    from tikiagent.providers.llm.request import structured_messages
    original = [{"role": "system", "content": "固定规则"}, {"role": "user", "content": "任务"}]
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    first = structured_messages(original, schema)
    assert structured_messages(first, schema) == first
    assert first[0]["content"].startswith("固定规则")
    assert original[0]["content"] == "固定规则"
