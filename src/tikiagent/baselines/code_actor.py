"""Plan/Verify 基线使用的 Code Actor 适配。"""

from __future__ import annotations

from tikiagent.orchestration.contracts import ActorResult, Plan
from tikiagent.runtime.models import MaxStepsExceeded
from tikiagent.runtime.react import ReActAgent


class ReActCodeActor:
    """隔离 ReAct 内部消息，只向外传递结构化 ActorResult。"""

    def __init__(self, agent: ReActAgent) -> None:
        self.agent = agent
        self.max_steps = agent.max_steps

    def execute(
        self,
        *,
        instruction: str,
        plan: Plan,
        acceptance_criteria: list[str],
    ) -> ActorResult:
        task = (
            f"目标：{plan.goal}\n"
            f"计划步骤：{plan.model_dump_json()}\n"
            "验收标准：\n- "
            + "\n- ".join(acceptance_criteria)
            + f"\n本轮 Planner 指令：{instruction}"
        )
        try:
            result = self.agent.run(task)
        except MaxStepsExceeded as error:
            return ActorResult(
                completed=False,
                summary=str(error),
                steps=self.agent.max_steps,
            )

        return ActorResult(
            # 这里仅表示 Actor 正常交付，不代表任务验收通过。
            completed=True,
            summary=result.final_text,
            steps=result.steps,
            tool_results=tuple(
                item.model_dump(mode="json")
                for item in result.tool_results
            ),
        )
