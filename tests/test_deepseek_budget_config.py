"""正式预算、供应商协议、完整交互与恢复边界的离线回归。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tikiagent.application.bootstrap import ApplicationRuntimeFactory
from tikiagent.application.events import EventBus
from tikiagent.application.models import EventScope
from tikiagent.context.compression.models import ContextBudget
from tikiagent.context.compression.llm import SummaryEngine, LLMLocalCompressor
from tikiagent.context.memory.models import LocalMemory
from tikiagent.context.models import BaseContext, WorkingMemory
from tikiagent.context.preparation import ContextRuntime, ContextBudgetExceeded
from tikiagent.harness.persistence.trace import JsonlTraceStore
from tikiagent.providers.llm.config import ModelSettings
from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient
from tikiagent.providers.llm.staged import StageModel
from tikiagent.runtime.policy import AgentPolicy, OUTPUT_LIMITS, thinking_effort


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    """本文件配置隔离，不读取用户密钥或借用本地 .env。"""
    import os
    for name in list(os.environ):
        if name.startswith("TIKI_"):
            monkeypatch.delenv(name)


def sdk_fixture():
    from test_openai_compatible import DumpableMessage
    requests = []
    class Message(DumpableMessage):
        def model_dump(self, **kwargs):
            return {**super().model_dump(**kwargs), "reasoning_content": "private protocol reasoning"}
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=Message(), finish_reason="tool_calls")])
    return requests, SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.mark.parametrize("stage,effort", [("router", "none"), ("supervisor", "high"),
    ("code_agent", "high"), ("code_final", "low"), ("research_agent", "low"),
    ("research_final", "low"), ("verifier", "high"), ("verifier_final", "low"), ("summary", "low"), ("chat", "low")])
def test_deepseek_stage_parameters_and_response_fields(stage, effort):
    requests, sdk = sdk_fixture()
    root = OpenAICompatibleClient(ModelSettings("fake", "http://offline", "deepseek-v4-flash", api_style="deepseek"), client=sdk)
    response = StageModel(root, stage).complete([], [])
    assert requests[0]["max_tokens"] == OUTPUT_LIMITS[stage]
    assert requests[0]["extra_body"]["thinking"]["type"] == ("disabled" if effort == "none" else "enabled")
    assert requests[0].get("reasoning_effort") == (None if effort == "none" else effort)
    assert response.assistant_message["reasoning_content"] == "private protocol reasoning"


def test_generic_api_never_receives_deepseek_parameters():
    requests, sdk = sdk_fixture()
    root = OpenAICompatibleClient(ModelSettings("fake", "http://offline", "vllm"), client=sdk)
    StageModel(root, "code_agent").complete([], [])
    assert "reasoning_effort" not in requests[0] and "extra_body" not in requests[0]


def test_stage_override_is_used_and_invalid_effort_is_rejected(monkeypatch):
    monkeypatch.setenv("TIKI_OUTPUT_CODE_AGENT", "12345")
    monkeypatch.setenv("TIKI_THINKING_CODE_AGENT", "max")
    requests, sdk = sdk_fixture()
    root = OpenAICompatibleClient(ModelSettings("fake", "http://offline", "deepseek", api_style="deepseek"), client=sdk)
    StageModel(root, "code_agent").complete([], [])
    assert requests[0]["max_tokens"] == 12345 and requests[0]["reasoning_effort"] == "max"
    monkeypatch.setenv("TIKI_THINKING_CODE_AGENT", "unknown")
    with pytest.raises(ValueError):
        thinking_effort("code_agent")


@pytest.mark.parametrize("host,expected", [("https://api.deepseek.com", "deepseek"),
    ("https://api.deepseek.com.evil.invalid", "openai"), ("http://localhost:8000/v1", "openai")])
def test_api_auto_detection_is_exact_host(monkeypatch, tmp_path, host, expected):
    for name, value in {"TIKI_LLM_API_KEY": "fake", "TIKI_LLM_BASE_URL": host, "TIKI_LLM_MODEL": "model"}.items():
        monkeypatch.setenv(name, value)
    assert ModelSettings.from_env(tmp_path / "missing").api_style == expected


def test_factory_injects_policy_and_preserves_context_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKI_CONTEXT_BASE_BUDGET", "20000")
    monkeypatch.setenv("TIKI_CONTEXT_LOCAL_BUDGET", "30000")
    monkeypatch.setenv("TIKI_CONTEXT_RECENT_INTERACTIONS", "6")
    monkeypatch.setenv("TIKI_CONTEXT_RECENT_TOKENS", "12000")
    monkeypatch.setenv("TIKI_CONTEXT_COMPRESSION_RATIO", "0.8")
    monkeypatch.setenv("TIKI_BUDGET_CODE_STEPS", "17")
    factory = ApplicationRuntimeFactory(tmp_path / "data", env_file=tmp_path / "missing")
    graph = factory._build_workflow("s", "w")
    assert graph.code_agent.agent.max_steps == 17
    assert graph.code_agent.agent.max_tool_calls == 32
    assert graph.supervisor.max_steps == 20 and graph.supervisor.max_tool_calls == 32
    assert factory.policy == AgentPolicy(code_steps=17)
    runtime = graph.code_agent.agent.context_runtime
    for phase, output in [("coding", 32768), ("finalization", 16384)]:
        prepared = runtime.prepare(base_context=BaseContext(agent="code_agent", working_memory=WorkingMemory(
            task="test", phase=phase, instruction="test")), local_memory=LocalMemory(),
            registry=graph.code_agent.agent.dispatcher.registry)
        assert runtime.budget.model_context_limit == 129072
        assert runtime.budget.base_context_budget == 20000
        assert runtime.budget.local_messages_budget == 30000
        assert runtime.budget.recent_interaction_limit == 6 and runtime.budget.recent_tokens_budget == 12000
        assert runtime.budget.compression_trigger_ratio == 0.8
        assert prepared.usage.available_input_budget == 129072 - output


@pytest.mark.parametrize("key,value", [("TIKI_LLM_CONTEXT_LIMIT", "32000"),
    ("TIKI_OUTPUT_CODE_AGENT", "200000"), ("TIKI_CONTEXT_LOCAL_BUDGET", "0"),
    ("TIKI_CONTEXT_COMPRESSION_RATIO", "1.1"), ("TIKI_THINKING_ROUTER", "bad")])
def test_invalid_configuration_fails_before_any_request(tmp_path, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        ApplicationRuntimeFactory(tmp_path / "data", env_file=tmp_path / "missing")


def test_reasoning_is_counted_and_recent_fields_survive_compression():
    from test_context_runtime import interaction, registry, base_context
    from test_llm_compression import Summarizer
    old, recent = interaction(0, 6000), interaction(1, 20)
    old.assistant_message["reasoning_content"] = "old private reasoning"
    recent.assistant_message["reasoning_content"] = "recent private reasoning"
    source = LocalMemory(recent_interactions=[old, recent])
    model = Summarizer()
    result = LLMLocalCompressor(SummaryEngine(model)).compress(source, recent_interaction_limit=1, task="test")
    assert result.changed and result.memory.recent_interactions == [recent]
    assert "old private reasoning" not in str(model.requests)
    runtime = ContextRuntime()
    call = runtime.prepare(base_context=base_context(), local_memory=result.memory, registry=registry())
    assert any(m.get("reasoning_content") == "recent private reasoning" for m in call.messages)
    before = call.usage.total_call_usage
    recent.assistant_message["reasoning_content"] *= 100
    after = runtime.prepare(base_context=base_context(), local_memory=result.memory, registry=registry()).usage.total_call_usage
    assert after > before


def test_eight_recent_interactions_are_kept_as_atomic_pairs():
    from test_context_runtime import interaction, registry, base_context
    from test_llm_compression import Summarizer
    runtime = ContextRuntime(budget=ContextBudget.from_env(model_context_limit=129072, reserved_output_tokens=16384)
        .model_copy(update={"local_messages_budget": 5000}),
        local_compressor=LLMLocalCompressor(SummaryEngine(Summarizer())))
    memory = LocalMemory(recent_interactions=[interaction(i, 3000) for i in range(10)])
    call = runtime.prepare(base_context=base_context(), local_memory=memory, registry=registry())
    assert len(call.local_memory.recent_interactions) == 8
    assert call.local_memory.recent_interactions[0].interaction_id == "interaction-2"
    assert len(memory.recent_interactions) == 10
    assert "call-0" not in {m.get("tool_call_id") for m in call.messages}


def test_large_protected_context_stops_without_compressing_task():
    from test_context_runtime import registry
    task = "准确任务约束" * 2000
    original = BaseContext(agent="code_agent", working_memory=WorkingMemory(task=task, phase="coding", instruction="test"))
    runtime = ContextRuntime(budget=ContextBudget(model_context_limit=5000, reserved_output_tokens=500))
    with pytest.raises(ContextBudgetExceeded):
        runtime.prepare(base_context=original, local_memory=LocalMemory(), registry=registry())
    assert original.working_memory.task == task


def test_reasoning_redaction_keeps_only_numeric_budget_metrics(tmp_path):
    bus = EventBus()
    data = {"reasoning_content": "private", "nested": {"Reasoning_Content": "private"},
        "max_output_tokens": 32768, "reasoning_tokens": 123, "api_key": "fake", "token": "fake",
        "completion_tokens_details": {"reasoning_tokens": 45, "token": "fake"},
        "prompt_tokens_details": {"cached_tokens": 67, "reasoning_content": "private"}}
    event = bus.emit("model_response", scope=EventScope(session_id="s", workspace_id="w"),
        source="test", correlation_id="t", message="test", data=data)
    assert event.data["reasoning_tokens"] == 123 and event.data["max_output_tokens"] == 32768
    assert event.data["completion_tokens_details"]["reasoning_tokens"] == 45
    assert event.data["prompt_tokens_details"]["cached_tokens"] == 67
    assert "private" not in event.model_dump_json() and "fake" not in event.model_dump_json()
    trace = JsonlTraceStore(tmp_path / "trace.jsonl")
    stored = trace.append(run_id="t", event_type="model_response", details=data)
    assert "private" not in stored.model_dump_json()


def test_supervisor_retains_reasoning_when_reconstructing_control_messages():
    from test_planning_supervisor import workflow, plan, stop
    response = plan("a")
    response = replace(response, assistant_message={"role": "assistant", "reasoning_content": "protocol data"})
    model, _, graph = workflow([response, stop()])
    graph.invoke("test")
    assert any(m.get("reasoning_content") == "protocol data" for m in model.requests[1])
    assert "remaining_rounds" in str(model.requests[1])


def test_pending_approval_roundtrip_retains_reasoning_and_frozen_steps(tmp_path):
    from tikiagent.context.memory.history import JsonlHistoryStore
    from test_multi_agent_resume import build_workflow, ScriptedModel, first_response, final_response
    from tikiagent.harness.permissions.models import ApprovalDecision
    response = first_response()
    response = replace(response, assistant_message={"role": "assistant", "reasoning_content": "protocol data"})
    workspace, first = build_workflow(tmp_path, ScriptedModel([response]),
        history_store=JsonlHistoryStore(tmp_path / "history.jsonl"))
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    paused = first.invoke("生成 output.txt", task_id="t", session_id="s")
    cp = first.code_agent.agent.execution_coordinator.checkpoint_store.load(paused["runtime_checkpoint_id"])
    assert cp.react_snapshot.pending_assistant_message["reasoning_content"] == "protocol data"
    assert cp.react_snapshot.local_memory["recent_interactions"] == []  # ASK 不生成半组交互。
    second_model = ScriptedModel([final_response()])
    _, second = build_workflow(tmp_path, second_model)
    second.code_agent.agent.max_steps = 16
    request = cp.approval_request
    completed = second.resume(cp.checkpoint_id, expected_revision=cp.revision, approval_decision=ApprovalDecision(
        request_id=request.request_id, approved=True, scope=request.scope, fingerprint=request.fingerprint))
    assert completed["status"] == "completed"
    assert any(m.get("reasoning_content") == "protocol data" for m in second_model.requests[0])
    assert '"max_steps": 4' in str(second_model.requests[0])


def test_structured_final_keeps_deepseek_settings_and_calls_once():
    from pydantic import BaseModel
    class Report(BaseModel):
        answer: str
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"answer":"done"}'), finish_reason="stop")])
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    root = OpenAICompatibleClient(ModelSettings("fake", "http://offline", "deepseek", api_style="deepseek"), client=sdk)
    result = StageModel(root, "verifier_final").complete_structured_once([], Report)
    assert result.answer == "done" and len(requests) == 1
    assert requests[0]["max_tokens"] == 16384 and requests[0]["reasoning_effort"] == "low"
    assert requests[0]["extra_body"] == {"thinking": {"type": "enabled"}}


def test_resume_does_not_expand_frozen_work_rounds(tmp_path):
    from test_multi_agent_resume import build_workflow, ScriptedModel, first_response, final_response
    from tikiagent.context.memory.history import JsonlHistoryStore
    from tikiagent.harness.permissions.models import ApprovalDecision
    from tikiagent.providers.llm.models import ModelResponse, ModelToolCall
    workspace, first = build_workflow(tmp_path, ScriptedModel([first_response()]),
        history_store=JsonlHistoryStore(tmp_path / "history.jsonl"))
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    paused = first.invoke("生成 output.txt", task_id="t", session_id="s")
    cp = first.code_agent.agent.execution_coordinator.checkpoint_store.load(paused["runtime_checkpoint_id"])
    responses = [ModelResponse(assistant_message={"role": "assistant"},
        tool_calls=(ModelToolCall(f"list-{i}", "list_files", f'{{"path":".","recursive":{str(bool(i % 2)).lower()}}}'),))
        for i in range(3)] + [final_response()]
    model = ScriptedModel(responses)
    _, second = build_workflow(tmp_path, model)
    second.code_agent.agent.max_steps = 16
    request = cp.approval_request
    completed = second.resume(cp.checkpoint_id, expected_revision=cp.revision, approval_decision=ApprovalDecision(
        request_id=request.request_id, approved=True, scope=request.scope, fingerprint=request.fingerprint))
    assert completed["status"] == "completed"
    assert len(model.requests) == 4  # 三轮剩余工作 + 一次收尾，不是升级后的15轮。
    assert "执行已停止：" in str(model.requests[-1])
