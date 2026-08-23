"""把现有 ReAct Agent 适配为不同工作流的 Code 执行组件。"""

from tikiagent.agents.react import MaxStepsExceeded, ReActAgent
from tikiagent.context.models import BaseContext
from tikiagent.orchestration.models import (
    ActorResult,
    CodeResult,
    Handoff,
    Plan,
)


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


class MultiAgentCodeAgent:
    """执行 Supervisor Handoff，只返回结构化 CodeResult。"""

    def __init__(self, agent: ReActAgent) -> None:
        self.agent = agent
        self.max_steps = agent.max_steps

    def run(
        self,
        *,
        handoff: Handoff,
        base_context: BaseContext,
    ) -> CodeResult:
        if handoff.to_agent != "code_agent":
            raise ValueError("CodeAgent 收到了错误目标的 Handoff")
        if base_context.agent != "code_agent":
            raise ValueError("CodeAgent 收到了错误 Profile 的 Base Context")

        # ReActAgent.run() 会为本次执行创建局部 messages；Base Context 只作为
        # 本轮初始输入，不接收其他 Agent 的内部 messages。
        try:
            run_result = self.agent.run(
                base_context.render(),
                base_context=base_context,
            )
        except MaxStepsExceeded as error:
            return CodeResult(
                handoff_id=handoff.handoff_id,
                summary=str(error),
                completed=False,
                steps=self.agent.max_steps,
                context_refs_used=handoff.context_refs,
            )

        tool_results = tuple(
            item.model_dump(mode="json") for item in run_result.tool_results
        )
        changed_files = sorted(
            {
                str(item.output["path"])
                for item in run_result.tool_results
                if item.ok
                and item.tool_name in {"write_file", "edit_file"}
                and isinstance(item.output, dict)
                and isinstance(item.output.get("path"), str)
            }
        )
        tests_run = [
            (
                f"command={item.output.get('command')} "
                f"exit_code={item.output.get('exit_code')} "
                f"timed_out={item.output.get('timed_out')}"
            )
            for item in run_result.tool_results
            if item.ok
            and item.tool_name == "run_command"
            and isinstance(item.output, dict)
        ]
        return CodeResult(
            handoff_id=handoff.handoff_id,
            summary=run_result.final_text,
            completed=True,
            steps=run_result.steps,
            changed_files=changed_files,
            tests_run=tests_run,
            context_refs_used=handoff.context_refs,
            tool_results=tool_results,
        )
