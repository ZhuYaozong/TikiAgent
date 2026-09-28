"""Supervisor 停止原因、旧 State 迁移以及有限模型请求配置。"""

import pytest

from tikiagent.agents.supervisor import SupervisorAgent
from tikiagent.orchestration.contracts import Handoff, SupervisorPlan
from tikiagent.orchestration.state import create_multi_agent_state, restore_tiki_state, serialize_tiki_state
from tikiagent.providers.llm.config import ModelSettings
from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient


def test_supervisor_stops_without_model_call_when_runtime_has_tripped():
    agent = SupervisorAgent(object())
    state = create_multi_agent_state(task="task", workspace_id="w", max_steps=12, max_delegations=5)
    state["required_specialists"] = ["code_agent"]
    state["specialist_results"] = {"code_agent": {"stop_reason": "repeated_tool_failure", "summary": "同一命令持续失败"}}
    decision = agent.decide(state)
    assert decision.action == "stop"
    assert "repeated_tool_failure" in decision.reason


def test_old_state_defaults_to_artifact_and_restores_budget_fields():
    state = create_multi_agent_state(task="task", workspace_id="w", max_steps=12, max_delegations=5)
    state["supervisor_plan"] = SupervisorPlan(goal="task", required_specialists=["code_agent"], acceptance_criteria=["verified"])
    state["latest_handoff"] = Handoff(from_agent="supervisor", to_agent="code_agent", instruction="task")
    payload = serialize_tiki_state(state)
    payload.pop("code_tool_call_count")
    payload.pop("max_code_tool_calls")
    payload["supervisor_plan"].pop("code_task_mode")
    payload["latest_handoff"].pop("delivery_mode")
    restored = restore_tiki_state(payload)
    assert restored["code_tool_call_count"] == 0
    assert restored["max_code_tool_calls"] == 60
    assert restored["supervisor_plan"].code_task_mode == "artifact"
    assert restored["latest_handoff"].delivery_mode == "artifact"


def test_sdk_receives_timeout_and_retry_budget(monkeypatch):
    captured = {}
    def factory(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr("tikiagent.providers.llm.openai_compatible.OpenAI", factory)
    OpenAICompatibleClient(ModelSettings(api_key="dummy", base_url="http://localhost", model="fake", timeout_seconds=15, max_retries=0))
    assert captured["timeout"] == 15
    assert captured["max_retries"] == 0


@pytest.mark.parametrize("seconds,retries", [(0, 1), (float("inf"), 1), (60, -1), (60, 100)])
def test_model_limits_reject_unbounded_or_invalid_configuration(seconds, retries):
    with pytest.raises(ValueError):
        ModelSettings(api_key="dummy", base_url="http://localhost", model="fake", timeout_seconds=seconds, max_retries=retries)
