"""ResearchAgent 的来源证据、预算和上下文隔离测试。"""

from typing import Any

from pydantic import BaseModel

from tikiagent.agents.research import ResearchAgent
from tikiagent.harness.dispatcher import Dispatcher
from tikiagent.harness.registry import RegisteredTool, ToolRegistry
from tikiagent.llm.models import ModelResponse, ModelToolCall
from tikiagent.orchestration.models import Handoff


class SearchArgs(BaseModel):
    query: str


def dispatcher() -> Dispatcher:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            "web_search",
            "search",
            SearchArgs,
            lambda query: {
                "query": query,
                "results": [
                    {
                        "title": "Release",
                        "url": "https://example.com/release",
                        "snippet": "new graph features",
                    }
                ],
            },
        )
    )
    return Dispatcher(registry)


class ScriptedModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tool_schemas) -> ModelResponse:
        del messages, tool_schemas
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=(
                    ModelToolCall(
                        "search-call-1",
                        "web_search",
                        '{"query":"latest agent framework"}',
                    ),
                ),
            )
        return ModelResponse(
            assistant_message={"role": "assistant", "content": "draft"},
            final_text="draft",
        )


class ScriptedStructuredModel:
    def __init__(self, source_url: str) -> None:
        self.source_url = source_url

    def complete_structured(self, messages, response_type):
        del messages
        return response_type.model_validate(
            {
                "summary": "summary",
                "findings": ["finding"],
                "sources": [
                    {
                        "title": "model title",
                        "url": self.source_url,
                        "snippet": "model snippet",
                    }
                ],
                "unresolved_questions": [],
            }
        )


def handoff() -> Handoff:
    return Handoff(
        handoff_id="handoff-1",
        from_agent="supervisor",
        to_agent="research_agent",
        instruction="research",
    )


def test_result_links_handoff_and_search_observation() -> None:
    agent = ResearchAgent(
        model=ScriptedModel(),
        structured_model=ScriptedStructuredModel(
            "https://example.com/release"
        ),
        dispatcher=dispatcher(),
    )

    result = agent.run(handoff())

    assert result.handoff_id == "handoff-1"
    assert result.result_id
    assert result.sources[0].observation_id == "search-call-1"
    assert result.observations[0].urls == [
        "https://example.com/release"
    ]
    assert "messages" not in result.model_dump()


def test_hallucinated_source_is_replaced_by_observed_source() -> None:
    agent = ResearchAgent(
        model=ScriptedModel(),
        structured_model=ScriptedStructuredModel(
            "https://invented.example/source"
        ),
        dispatcher=dispatcher(),
    )

    result = agent.run(handoff())

    assert [source.url for source in result.sources] == [
        "https://example.com/release"
    ]


class RepeatingSearchModel:
    def complete(self, messages, tool_schemas) -> ModelResponse:
        del messages, tool_schemas
        return ModelResponse(
            assistant_message={"role": "assistant", "content": None},
            tool_calls=(
                ModelToolCall(
                    "repeated-search",
                    "web_search",
                    '{"query":"agent"}',
                ),
            ),
        )


def test_tool_budget_prevents_unbounded_searches() -> None:
    agent = ResearchAgent(
        model=RepeatingSearchModel(),
        structured_model=ScriptedStructuredModel(
            "https://example.com/release"
        ),
        dispatcher=dispatcher(),
        max_steps=3,
        max_searches=1,
    )

    result = agent.run(handoff())

    assert len(result.observations) == 1
    assert result.sources[0].observation_id == "repeated-search"


def test_failed_search_returns_verifiable_empty_result() -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            "web_search",
            "search",
            SearchArgs,
            lambda query: {"query": query, "results": []},
        )
    )
    agent = ResearchAgent(
        model=ScriptedModel(),
        structured_model=ScriptedStructuredModel("https://none.example"),
        dispatcher=Dispatcher(registry),
    )

    result = agent.run(handoff())

    assert result.sources == []
    assert result.observations[0].urls == []
    assert "未取得可验证来源" in result.unresolved_questions
