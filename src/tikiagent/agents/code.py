"""接受 Supervisor Handoff 的代码专业 Agent。"""

from __future__ import annotations

from tikiagent.context.models import BaseContext
from tikiagent.harness.permissions.models import ApprovalDecision
from tikiagent.harness.persistence.checkpoint import WorkflowResumeSnapshot
from tikiagent.harness.persistence.recovery import ReconcileResult, RecoveryDecision
from tikiagent.harness.scope import ExecutionContext
from tikiagent.orchestration.contracts import CodeResult, Handoff
from tikiagent.runtime.models import AgentRunPause, AgentRunResult, MaxStepsExceeded
from tikiagent.runtime.react import ReActAgent
from tikiagent.runtime.resumable import ResumableReActAgent


class MultiAgentCodeAgent:
    """执行 Supervisor Handoff，只返回结构化 CodeResult。"""

    def __init__(self, agent: ReActAgent) -> None:
        self.agent = agent
        self.max_steps = agent.max_steps
        self.supports_resume = isinstance(agent, ResumableReActAgent)

    def run(
        self,
        *,
        handoff: Handoff,
        base_context: BaseContext,
        execution_context: ExecutionContext | None = None,
        workflow_snapshot: WorkflowResumeSnapshot | None = None,
    ) -> CodeResult | AgentRunPause:
        if handoff.to_agent != "code_agent":
            raise ValueError("CodeAgent 收到了错误目标的 Handoff")
        if base_context.agent != "code_agent":
            raise ValueError("CodeAgent 收到了错误 Profile 的 Base Context")

        # ReActAgent.run() 会为本次执行创建局部 messages；Base Context 只作为
        # 本轮初始输入，不接收其他 Agent 的内部 messages。
        try:
            if self.supports_resume:
                run_result = self.agent.run(
                    base_context.render(),
                    base_context=base_context,
                    execution_context=execution_context,
                    workflow_snapshot=workflow_snapshot,
                )
            else:
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

        if isinstance(run_result, AgentRunPause):
            return run_result
        return self._to_code_result(handoff, run_result)

    def resume(
        self,
        *,
        handoff: Handoff,
        checkpoint_id: str,
        expected_revision: int,
        approval_decision: ApprovalDecision | None = None,
        recovery_decision: RecoveryDecision | None = None,
        reconciliation: ReconcileResult | None = None,
    ) -> CodeResult | AgentRunPause:
        """仅供 Multi-Agent Graph 的 Resume Entry Node 调用。"""

        if not isinstance(self.agent, ResumableReActAgent):
            raise RuntimeError("当前 CodeAgent 不支持 Resume")
        run_result = self.agent.resume(
            checkpoint_id,
            expected_revision=expected_revision,
            approval_decision=approval_decision,
            recovery_decision=recovery_decision,
            reconciliation=reconciliation,
        )
        if isinstance(run_result, AgentRunPause):
            return run_result
        return self._to_code_result(handoff, run_result)

    @staticmethod
    def _to_code_result(
        handoff: Handoff,
        run_result: AgentRunResult,
    ) -> CodeResult:
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
