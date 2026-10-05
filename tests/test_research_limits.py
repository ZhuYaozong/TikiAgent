"""研究预算扩容回归：只用离线模型，验证容量和安全上限同时生效。"""

import hashlib
import json

import pytest
from pydantic import ValidationError

from tikiagent.agents.research import CompactResearchDraft, ResearchAgent
from tikiagent.context.profiles import DEFAULT_CONTEXT_PROFILES
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall
from tikiagent.runtime.policy import AgentPolicy, output_limit
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.registry import RegisteredTool, ToolRegistry
from test_research_agent import (
    ScriptedModel,
    ScriptedStructuredModel,
    SearchArgs,
    dispatcher,
    handoff,
)


def test_expanded_research_defaults_and_environment_overrides(monkeypatch):
    # 不读取用户 .env；验证默认值与显式覆盖的优先级。
    for key in AgentPolicy.model_fields:
        monkeypatch.delenv(f"TIKI_BUDGET_{key.upper()}", raising=False)
    policy = AgentPolicy.from_env()
    assert (policy.research_steps, policy.research_searches, policy.research_extracts) == (16, 8, 10)
    assert policy.task_web_tools == 40 and policy.model_requests == 128
    monkeypatch.delenv("TIKI_OUTPUT_RESEARCH_FINAL", raising=False)
    assert output_limit("research_final") == 16384
    agent = ResearchAgent(model=ScriptedModel(), structured_model=ScriptedStructuredModel(
        "https://example.com/release"), dispatcher=dispatcher())
    assert agent.max_steps == policy.research_steps
    assert agent.tool_limits == {"web_search": 8, "web_extract": 10}

    for key, value in {"RESEARCH_STEPS": 12, "RESEARCH_SEARCHES": 5,
                       "RESEARCH_EXTRACTS": 7, "TASK_WEB_TOOLS": 30}.items():
        monkeypatch.setenv(f"TIKI_BUDGET_{key}", str(value))
    configured = AgentPolicy.from_env()
    assert (configured.research_steps, configured.research_searches,
            configured.research_extracts, configured.task_web_tools) == (12, 5, 7, 30)


def test_long_summary_reaches_research_result_with_consistent_prompt():
    class LongSummary:
        calls = 0

        def complete_structured(self, messages, response_type):
            self.calls += 1
            prompt = json.dumps(messages, ensure_ascii=False)
            for rule in ("摘要不超过2000字符", "结论最多12条", "每条不超过800字符"):
                assert rule in prompt
            assert "不超过300字" not in prompt and "不超过120字" not in prompt
            ref = "s-" + hashlib.sha256(b"https://example.com/release").hexdigest()[:12]
            # 使用边界长度，确认原来的 600/240/6 校验上限已经扩容。
            return response_type(summary="研" * 2000, findings=[
                {"text": "结" * 800, "source_ids": [ref]} for _ in range(12)
            ], unresolved_questions=[], delivery_status="ready")

    summary = LongSummary()
    result = ResearchAgent(model=ScriptedModel(), structured_model=summary,
        dispatcher=dispatcher()).run(handoff())
    assert summary.calls == 1 and result.finalization_status == "completed"
    assert len(result.summary) == 2000 and len(result.findings) == 12
    assert all(len(item) == 800 for item in result.findings)
    assert result.sources[0].url == "https://example.com/release"


@pytest.mark.parametrize("field,value", [
    ("summary", "研" * 2001),
    ("findings", [{"text": "结" * 801, "source_ids": ["source"]}]),
    ("findings", [{"text": "结", "source_ids": ["source"]}] * 13),
])
def test_expanded_summary_still_rejects_over_limit_output(field, value):
    payload = {"summary": "研究摘要", "findings": [], "delivery_status": "partial"}
    payload[field] = value
    with pytest.raises(ValidationError):
        CompactResearchDraft.model_validate(payload)


def test_profile_and_schema_share_summary_limits():
    profile = "\n".join(DEFAULT_CONTEXT_PROFILES["research_agent"].phase_rules["research_synthesis"])
    assert "2000字符" in profile and "12条" in profile and "800字符" in profile
    schema = CompactResearchDraft.model_json_schema()
    assert schema["properties"]["summary"]["maxLength"] == 2000
    assert schema["properties"]["findings"]["maxItems"] == 12
    assert schema["$defs"]["ResearchFinding"]["properties"]["text"]["maxLength"] == 800


@pytest.mark.parametrize("tool_name,limit", [("web_search", 8), ("web_extract", 10)])
def test_expanded_work_limits_still_finalize_once(tool_name, limit):
    executed = []
    registry = ToolRegistry()

    def handler(query):
        executed.append(query)
        return {"query": query, "results": [{"title": query,
            "url": f"https://example.com/{query}", "snippet": "已读取的证据"}]}

    # 这里只测试次数边界，工具参数与返回值由离线桩提供。
    registry.register(RegisteredTool(tool_name, "离线工具", SearchArgs, handler))

    class Searching:
        calls = 0

        def complete(self, messages, tool_schemas):
            self.calls += 1
            call = ModelToolCall(f"call-{self.calls}", tool_name,
                json.dumps({"query": f"query-{self.calls}"}))
            return ModelResponse(assistant_message={"role": "assistant", "content": None},
                tool_calls=(call,))

    class Summary:
        calls = 0

        def complete_structured(self, messages, response_type):
            self.calls += 1
            return response_type(summary="保留已取得的证据", findings=[], delivery_status="partial")

    model, summary = Searching(), Summary()
    result = ResearchAgent(model=model, structured_model=summary,
        dispatcher=Dispatcher(registry)).run(handoff())
    assert len(executed) == limit and model.calls == limit
    assert result.stop_reason == "tool_budget_exhausted"
    assert summary.calls == 1 and result.finalization_status == "completed"
