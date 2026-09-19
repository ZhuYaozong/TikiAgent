"""ReAct Code Actor 的结构化 Handoff 测试。"""

from tikiagent.agents.code import MultiAgentCodeAgent
from tikiagent.baselines.code_actor import ReActCodeActor
from tikiagent.context.memory.models import HistoryRecord
from tikiagent.context.models import BaseContext, WorkingMemory
from tikiagent.orchestration.contracts import Handoff, Plan
from tikiagent.runtime.models import AgentRunResult, MaxStepsExceeded
from tikiagent.tools.models import ToolResult


class SuccessfulAgent:
    max_steps = 5

    def run(self, task: str) -> AgentRunResult:
        assert "验收标准" in task
        return AgentRunResult(
            final_text="修复完成",
            steps=2,
            tool_results=(
                ToolResult(
                    tool_call_id="call_1",
                    tool_name="read_file",
                    ok=True,
                    output={"content": "done"},
                ),
            ),
            messages=(
                {"role": "system", "content": "内部上下文"},
                {"role": "assistant", "content": "修复完成"},
            ),
        )


class ExhaustedAgent:
    max_steps = 3

    def run(self, task: str) -> AgentRunResult:
        raise MaxStepsExceeded("Agent 超过最大步数：3")


def plan() -> Plan:
    return Plan(
        goal="修复代码",
        steps=["修复", "测试"],
        acceptance_criteria=["测试通过"],
    )


def test_actor_handoff_excludes_internal_messages() -> None:
    actor = ReActCodeActor(SuccessfulAgent())

    result = actor.execute(
        instruction="执行修复",
        plan=plan(),
        acceptance_criteria=["测试通过"],
    )

    assert actor.max_steps == 5
    assert result.completed is True
    assert result.summary == "修复完成"
    assert result.steps == 2
    assert result.tool_results[0]["tool_name"] == "read_file"
    assert "messages" not in result.model_dump()


def test_actor_max_steps_becomes_structured_result() -> None:
    actor = ReActCodeActor(ExhaustedAgent())

    result = actor.execute(
        instruction="执行修复",
        plan=plan(),
        acceptance_criteria=["测试通过"],
    )

    assert result.completed is False
    assert result.steps == 3
    assert "最大步数" in result.summary


class SuccessfulMultiAgent:
    max_steps = 6

    def run(self, task: str, *, base_context=None) -> AgentRunResult:
        assert base_context is not None
        assert base_context.agent == "code_agent"
        assert "research summary" in task
        return AgentRunResult(
            final_text="网页完成",
            steps=3,
            tool_results=(
                ToolResult(
                    tool_call_id="write-1",
                    tool_name="write_file",
                    ok=True,
                    output={"path": "comparison.html"},
                ),
                ToolResult(
                    tool_call_id="test-1",
                    tool_name="run_command",
                    ok=True,
                    output={
                        "command": ["python", "check.py"],
                        "exit_code": 0,
                        "timed_out": False,
                    },
                ),
            ),
            messages=({"role": "system", "content": "private"},),
        )


def test_multi_agent_code_result_links_handoff_without_messages() -> None:
    agent = MultiAgentCodeAgent(SuccessfulMultiAgent())
    handoff = Handoff(
        handoff_id="handoff-code-1",
        from_agent="supervisor",
        to_agent="code_agent",
        instruction="create report",
        context_refs=["research-result-1"],
    )
    base_context = BaseContext(
        agent="code_agent",
        working_memory=WorkingMemory(
            task="create report",
            phase="coding",
            instruction="create report",
            acceptance_criteria=["verified"],
            todos=[],
            relevant_history=[
                HistoryRecord(
                    record_id="research-result-1",
                    task_id="task-1",
                    session_id="session-1",
                    record_type="result",
                    producer="research_agent",
                    summary="research summary",
                )
            ],
        ),
    )

    result = agent.run(
        handoff=handoff,
        base_context=base_context,
    )

    assert result.handoff_id == "handoff-code-1"
    assert result.result_id
    assert result.changed_files == ["comparison.html"]
    assert "exit_code=0" in result.tests_run[0]
    assert result.context_refs_used == ["research-result-1"]
    assert "messages" not in result.model_dump()
