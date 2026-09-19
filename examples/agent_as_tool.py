"""独立演示 Agent-as-Tool；TikiAgent 主 Graph 不采用此模式。"""

from pydantic import BaseModel, ConfigDict, Field

from tikiagent.agents.research import ResearchAgent
from tikiagent.orchestration.contracts import Handoff
from tikiagent.providers.llm.config import ModelSettings
from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient
from tikiagent.providers.search.config import SearchSettings
from tikiagent.providers.search.tavily import TavilyProvider
from tikiagent.runtime.react import ReActAgent
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.registry import RegisteredTool, ToolRegistry
from tikiagent.tools.web import build_web_registry


class CallResearchAgentArgs(BaseModel):
    """Supervisor ToolCall 传给嵌套 ResearchAgent 的参数。"""

    model_config = ConfigDict(strict=True, extra="forbid")
    instruction: str = Field(min_length=1)


def main() -> None:
    model = OpenAICompatibleClient(ModelSettings.from_env())
    research = ResearchAgent(
        model=model,
        structured_model=model,
        dispatcher=Dispatcher(
            build_web_registry(TavilyProvider(SearchSettings.from_env()))
        ),
    )
    registry = ToolRegistry()

    def call_research_agent(instruction: str):
        handoff = Handoff(
            from_agent="supervisor",
            to_agent="research_agent",
            instruction=instruction,
        )
        return research.run(handoff).model_dump(mode="json")

    registry.register(
        RegisteredTool(
            "call_research_agent",
            "把 Web Research 任务委派给独立 ResearchAgent",
            CallResearchAgentArgs,
            call_research_agent,
        )
    )
    supervisor = ReActAgent(
        model=model,
        dispatcher=Dispatcher(registry),
        system_prompt=(
            "你是 Agent-as-Tool 教学示例中的 Supervisor。需要调研时"
            "调用 call_research_agent，观察结构化 Result 后回答。"
        ),
        max_steps=4,
    )
    result = supervisor.run("调研最近 Agent Framework 的一项重要变化。")
    print(result.final_text)


if __name__ == "__main__":
    main()
