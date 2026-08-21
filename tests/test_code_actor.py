"""ReAct Code Actor 的结构化 Handoff 测试。"""

from tikiagent.agents import (
    AgentRunResult,
    MaxStepsExceeded,
    ReActCodeActor,
)
from tikiagent.harness import ToolResult
from tikiagent.orchestration import Plan


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
