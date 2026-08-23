"""Candidate/Prepared 调用、预算、压缩和 Tool View 测试。"""

import json

import pytest
from pydantic import BaseModel

from tikiagent.context import (
    BaseContext,
    CallContextAssembler,
    ContextBudget,
    ContextMonitor,
    ContextRuntime,
    HistoryRecord,
    LocalMemory,
    PromptAssembler,
    ReActInteraction,
    RuleBasedBaseCompressor,
    RuleBasedLocalCompressor,
    ToolExposureGuard,
    ToolSelector,
    WorkingMemory,
)
from tikiagent.harness.registry import RegisteredTool, ToolRegistry


class EmptyArgs(BaseModel):
    pass


class StructuredAnswer(BaseModel):
    answer: str


def registry() -> ToolRegistry:
    value = ToolRegistry()
    for name in [
        "read_file",
        "list_files",
        "grep",
        "write_file",
        "edit_file",
        "run_command",
    ]:
        value.register(RegisteredTool(name, f"{name} description", EmptyArgs, lambda: None))
    return value


def interaction(index: int, size: int = 20) -> ReActInteraction:
    call_id = f"call-{index}"
    return ReActInteraction(
        interaction_id=f"interaction-{index}",
        assistant_message={
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        tool_messages=[
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps({"ok": True, "output": "x" * size}),
            }
        ],
    )


def base_context() -> BaseContext:
    records = [
        HistoryRecord(
            record_id=f"history-{index}",
            task_id="task-1",
            session_id="session-1",
            record_type="result",
            producer="code_agent",
            summary=("旧执行记录和重复诊断。" * 40) + str(index),
            payload=(
                {"durable_fact": "页面必须保留可追溯来源"}
                if index == 0
                else {}
            ),
        )
        for index in range(6)
    ]
    return BaseContext(
        agent="code_agent",
        working_memory=WorkingMemory(
            task="修复页面",
            phase="coding",
            instruction="修复并验证",
            acceptance_criteria=["测试通过"],
            relevant_history=records,
            protected_refs=["history-5"],
        ),
    )


def test_candidate_precedes_monitor_and_prepared_call() -> None:
    context = base_context()
    prompt = PromptAssembler().assemble("code_agent", "coding")
    tools = ToolSelector().select(
        agent="code_agent",
        phase="coding",
        registry=registry(),
    )
    candidate = CallContextAssembler().assemble(
        prompt=prompt,
        base_context=context,
        local_memory=LocalMemory(recent_interactions=[interaction(1)]),
        tool_view=tools,
        response_schema=StructuredAnswer.model_json_schema(),
    )
    usage = ContextMonitor().measure(candidate, ContextBudget())

    assert not hasattr(candidate, "usage")
    assert usage.response_schema_tokens > 0
    assert usage.total_call_usage == sum(
        [
            usage.prompt_tokens,
            usage.base_tokens,
            usage.history_tokens,
            usage.notepad_tokens,
            usage.local_tokens,
            usage.tool_schema_tokens,
            usage.response_schema_tokens,
        ]
    )
    assert usage.available_input_budget == 30_000


def test_runtime_compresses_then_reassembles_prepared_call() -> None:
    runtime = ContextRuntime(
        budget=ContextBudget(
            model_context_limit=5_000,
            reserved_output_tokens=500,
            base_context_budget=350,
            local_messages_budget=180,
            recent_interaction_limit=2,
        )
    )
    local = LocalMemory(
        recent_interactions=[interaction(index, 400) for index in range(5)]
    )

    prepared = runtime.prepare(
        base_context=base_context(),
        local_memory=local,
        registry=registry(),
        response_type=StructuredAnswer,
    )

    assert prepared.compression_actions == ["base", "local"]
    assert prepared.usage.total_over_budget is False
    assert prepared.base_context.working_memory.history_summary
    assert [
        item.record_id
        for item in prepared.base_context.working_memory.relevant_history
    ] == ["history-5"]
    assert len(prepared.local_memory.recent_interactions) == 2
    assert prepared.local_memory.summary
    assert prepared.notepad_candidates
    assert prepared.notepad_candidates[0].approved is False
    # Prepared 的 messages 必须来自压缩后部件的重新组装。
    assert prepared.local_memory.summary in prepared.messages[0]["content"]


def test_base_and_local_compressors_have_independent_protocols() -> None:
    base_result = RuleBasedBaseCompressor().compress(base_context())
    local_result = RuleBasedLocalCompressor().compress(
        LocalMemory(recent_interactions=[interaction(i) for i in range(4)]),
        recent_interaction_limit=2,
    )

    assert base_result.changed
    assert base_result.context.working_memory.history_summary
    assert local_result.changed
    assert len(local_result.memory.recent_interactions) == 2


def test_react_interaction_rejects_unpaired_tool_result() -> None:
    with pytest.raises(ValueError, match="完整配对"):
        ReActInteraction(
            interaction_id="broken",
            assistant_message={
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            tool_messages=[
                {"role": "tool", "tool_call_id": "other", "content": "{}"}
            ],
        )


def test_multiple_tool_calls_are_one_atomic_interaction() -> None:
    value = ReActInteraction(
        interaction_id="parallel-read",
        assistant_message={
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {
                    "id": "call-b",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ],
        },
        tool_messages=[
            {"role": "tool", "tool_call_id": "call-a", "content": "{}"},
            {"role": "tool", "tool_call_id": "call-b", "content": "{}"},
        ],
    )

    compressed = RuleBasedLocalCompressor().compress(
        LocalMemory(
            recent_interactions=[interaction(0), interaction(1), value]
        ),
        recent_interaction_limit=1,
    )

    assert compressed.memory.recent_interactions == [value]
    assert len(compressed.memory.recent_interactions[0].tool_messages) == 2


def test_dynamic_tool_view_and_exposure_guard_are_separate_from_permission() -> None:
    value = registry()
    coding = ToolSelector().select(
        agent="code_agent", phase="coding", registry=value
    )
    debugging = ToolSelector().select(
        agent="code_agent", phase="debugging", registry=value
    )

    assert "write_file" in coding.exposed_names
    assert "write_file" not in debugging.exposed_names
    assert ToolExposureGuard.allows("edit_file", debugging)
    assert not ToolExposureGuard.allows("write_file", debugging)
