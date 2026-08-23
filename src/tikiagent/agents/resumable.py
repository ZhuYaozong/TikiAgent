"""正式 Multi-Agent 使用的可暂停、可恢复 ReAct Runtime。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from tikiagent.agents.react import AgentRunResult, MaxStepsExceeded, ReActAgent
from tikiagent.context.local_memory import LocalMemoryManager
from tikiagent.context.models import (
    BaseContext,
    ContextUsage,
    LocalMemory,
    ToolView,
)
from tikiagent.harness.checkpoint import (
    CheckpointConflictError,
    PendingModelToolCall,
    ReActRunSnapshot,
    WorkflowResumeSnapshot,
)
from tikiagent.harness.coordinator import ExecutionCoordinator
from tikiagent.harness.models import (
    ApprovalDecision,
    ApprovalRequest,
    ExecutionContext,
    ToolError,
    ToolResult,
)
from tikiagent.harness.recovery import ReconcileResult, RecoveryDecision


@dataclass(frozen=True, slots=True)
class AgentRunPause:
    """ASK 或未知副作用恢复时交给外层 Graph 的暂停事实。"""

    status: Literal[
        "awaiting_approval",
        "recovery_required",
        "awaiting_reconcile",
    ]
    checkpoint_id: str
    revision: int
    approval_request: ApprovalRequest | None = None


AgentRunOutcome = AgentRunResult | AgentRunPause


class ResumableReActAgent(ReActAgent):
    """所有正式 ToolCall 都经过 ExecutionCoordinator，不走 legacy dispatch。"""

    def __init__(self, *args, execution_coordinator: ExecutionCoordinator, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.execution_coordinator = execution_coordinator

    def run(
        self,
        task: str,
        *,
        base_context: BaseContext | None = None,
        execution_context: ExecutionContext | None = None,
        workflow_snapshot: WorkflowResumeSnapshot | None = None,
    ) -> AgentRunOutcome:
        if execution_context is None or workflow_snapshot is None:
            raise ValueError(
                "ResumableReActAgent 需要 ExecutionContext 与 Workflow Snapshot"
            )
        context = base_context or self._default_context(task)
        if context.agent != "code_agent":
            raise ValueError("ResumableReActAgent 只接受 code_agent BaseContext")
        return self._run_loop(
            context=context,
            local=LocalMemoryManager(),
            tool_results=[],
            context_usages=[],
            phases=[],
            start_step=1,
            execution_context=execution_context,
            workflow_snapshot=workflow_snapshot,
            run_id=None,
        )

    def resume(
        self,
        checkpoint_id: str,
        *,
        expected_revision: int,
        approval_decision: ApprovalDecision | None = None,
        recovery_decision: RecoveryDecision | None = None,
        reconciliation: ReconcileResult | None = None,
    ) -> AgentRunOutcome:
        """恢复执行；调用方必须是 Graph 中的 Resume Entry Node。"""

        coordinator = self.execution_coordinator
        checkpoint = coordinator.checkpoint_store.load(checkpoint_id)
        if checkpoint.revision != expected_revision:
            raise CheckpointConflictError(
                f"Checkpoint revision 冲突：expected={expected_revision}, "
                f"actual={checkpoint.revision}"
            )
        if checkpoint.execution_state in {"completed", "denied"}:
            raise CheckpointConflictError("已完成 Checkpoint 不能重复 Resume")
        if checkpoint.execution_state == "executing":
            checkpoint = coordinator.mark_interrupted(
                checkpoint_id,
                expected_revision=checkpoint.revision,
            )

        coordinated = None
        if checkpoint.execution_state == "awaiting_approval":
            if approval_decision is None:
                return self._pause(checkpoint)
            coordinated = coordinator.resume_approval(
                checkpoint_id,
                decision=approval_decision,
                expected_revision=checkpoint.revision,
            )
            checkpoint = coordinated.checkpoint
        elif checkpoint.execution_state == "recovery_required":
            if recovery_decision is None:
                return self._pause(checkpoint)
            checkpoint = coordinator.apply_recovery(
                checkpoint_id,
                decision=recovery_decision,
                expected_revision=checkpoint.revision,
            )
            if checkpoint.execution_state == "pending":
                coordinated = coordinator.retry_confirmed_not_executed(
                    checkpoint_id,
                    expected_revision=checkpoint.revision,
                )
                checkpoint = coordinated.checkpoint
        if checkpoint is not None and checkpoint.execution_state == "awaiting_reconcile":
            if reconciliation is None:
                return self._pause(checkpoint)
            checkpoint = coordinator.reconcile(
                checkpoint_id,
                reconciliation=reconciliation,
                expected_revision=checkpoint.revision,
            )

        if coordinated is not None and coordinated.outcome.status == "awaiting_approval":
            assert checkpoint is not None
            return self._pause(checkpoint)
        if checkpoint is None or checkpoint.execution_state not in {
            "completed",
            "denied",
        }:
            raise CheckpointConflictError("Checkpoint 当前不能继续 ReAct")
        if checkpoint.tool_result is None:
            raise CheckpointConflictError("恢复继续前必须存在真实 ToolResult")
        return self._continue_from_checkpoint(checkpoint)

    def _run_loop(
        self,
        *,
        context: BaseContext,
        local: LocalMemoryManager,
        tool_results: list[ToolResult],
        context_usages: list[ContextUsage],
        phases: list[str],
        start_step: int,
        execution_context: ExecutionContext,
        workflow_snapshot: WorkflowResumeSnapshot,
        run_id: str | None,
    ) -> AgentRunOutcome:
        for step in range(start_step, self.max_steps + 1):
            prepared = self.context_runtime.prepare(
                base_context=context,
                local_memory=local.memory,
                registry=self.dispatcher.registry,
            )
            context = prepared.base_context
            local.replace(prepared.local_memory)
            context_usages.append(prepared.usage)
            phases.append(context.working_memory.phase)
            response = self.model.complete(
                messages=prepared.messages,
                tool_schemas=prepared.tool_view.schemas,
            )
            if response.tool_calls:
                calls = [
                    PendingModelToolCall(
                        tool_call_id=call.tool_call_id,
                        name=call.name,
                        arguments_json=call.arguments_json,
                    )
                    for call in response.tool_calls
                ]
                assistant = self._canonical_assistant_message(response)
                step_results: list[ToolResult] = []
                for index, call in enumerate(calls):
                    snapshot = self._snapshot(
                        task=context.render(),
                        step=step,
                        context=context,
                        local=local.memory,
                        tool_results=tool_results,
                        context_usages=context_usages,
                        phases=phases,
                        assistant_message=assistant,
                        pending_calls=calls,
                        pending_results=step_results,
                        next_tool_index=index,
                    )
                    result = self._execute_call(
                        call=call,
                        tool_view=prepared.tool_view,
                        execution_context=execution_context,
                        workflow_snapshot=workflow_snapshot,
                        react_snapshot=snapshot,
                        run_id=run_id,
                    )
                    if isinstance(result, AgentRunPause):
                        return result
                    step_results.append(result)
                    tool_results.append(result)
                self._append_interaction(local, step, assistant, step_results)
                context = self._transition_context(context, step_results)
                continue
            if response.final_text is not None:
                return AgentRunResult(
                    final_text=response.final_text,
                    steps=step,
                    tool_results=tuple(tool_results),
                    messages=tuple([*prepared.messages, response.assistant_message]),
                    context_usages=tuple(context_usages),
                    phases=tuple(phases),
                )
            raise RuntimeError("模型既没有返回 ToolCall，也没有最终文本")
        raise MaxStepsExceeded(f"Agent 超过最大步数：{self.max_steps}")

    def _continue_from_checkpoint(self, checkpoint) -> AgentRunOutcome:
        snapshot = checkpoint.react_snapshot
        context = BaseContext.model_validate(snapshot.base_context)
        local = LocalMemoryManager(LocalMemory.model_validate(snapshot.local_memory))
        tool_results = list(snapshot.tool_results)
        usages = [ContextUsage.model_validate(item) for item in snapshot.context_usages]
        phases = list(snapshot.phases)
        step_results = list(snapshot.pending_results)
        step_results.append(checkpoint.tool_result)
        tool_results.append(checkpoint.tool_result)
        execution_context = ExecutionContext(
            scope=checkpoint.scope,
            agent=checkpoint.agent,
            exposed_tools=checkpoint.exposed_tools,
        )
        for index in range(
            snapshot.next_tool_index + 1,
            len(snapshot.pending_tool_calls),
        ):
            call = snapshot.pending_tool_calls[index]
            next_snapshot = self._snapshot(
                task=snapshot.task,
                step=snapshot.step,
                context=context,
                local=local.memory,
                tool_results=tool_results,
                context_usages=usages,
                phases=phases,
                assistant_message=snapshot.pending_assistant_message,
                pending_calls=snapshot.pending_tool_calls,
                pending_results=step_results,
                next_tool_index=index,
            )
            result = self._execute_call(
                call=call,
                tool_view=ToolView(exposed_names=checkpoint.exposed_tools),
                execution_context=execution_context,
                workflow_snapshot=checkpoint.workflow_snapshot,
                react_snapshot=next_snapshot,
                run_id=checkpoint.identity.run_id,
            )
            if isinstance(result, AgentRunPause):
                return result
            step_results.append(result)
            tool_results.append(result)
        self._append_interaction(
            local,
            snapshot.step,
            snapshot.pending_assistant_message,
            step_results,
        )
        context = self._transition_context(context, step_results)
        return self._run_loop(
            context=context,
            local=local,
            tool_results=tool_results,
            context_usages=usages,
            phases=phases,
            start_step=snapshot.step + 1,
            execution_context=execution_context,
            workflow_snapshot=checkpoint.workflow_snapshot,
            run_id=checkpoint.identity.run_id,
        )

    def _execute_call(
        self,
        *,
        call: PendingModelToolCall,
        tool_view: ToolView,
        execution_context: ExecutionContext,
        workflow_snapshot: WorkflowResumeSnapshot,
        react_snapshot: ReActRunSnapshot,
        run_id: str | None,
    ) -> ToolResult | AgentRunPause:
        arguments = self._parse_arguments(call)
        if isinstance(arguments, ToolResult):
            return arguments
        scoped = execution_context.model_copy(
            update={"exposed_tools": tool_view.exposed_names}
        )
        coordinated = self.execution_coordinator.execute(
            {
                "tool_call_id": call.tool_call_id,
                "name": call.name,
                "arguments": arguments,
            },
            context=scoped,
            workflow_snapshot=workflow_snapshot,
            react_snapshot=react_snapshot,
            run_id=run_id,
        )
        if coordinated.outcome.status == "awaiting_approval":
            assert coordinated.checkpoint is not None
            return self._pause(coordinated.checkpoint)
        result = coordinated.outcome.tool_result
        assert result is not None
        return result

    @staticmethod
    def _snapshot(
        *,
        task: str,
        step: int,
        context: BaseContext,
        local: LocalMemory,
        tool_results: list[ToolResult],
        context_usages: list[ContextUsage],
        phases: list[str],
        assistant_message: dict[str, Any],
        pending_calls: list[PendingModelToolCall],
        pending_results: list[ToolResult],
        next_tool_index: int,
    ) -> ReActRunSnapshot:
        return ReActRunSnapshot(
            task=task,
            step=step,
            base_context=context.model_dump(mode="json"),
            local_memory=local.model_dump(mode="json"),
            tool_results=tool_results,
            context_usages=[item.model_dump(mode="json") for item in context_usages],
            phases=phases,
            pending_assistant_message=assistant_message,
            pending_tool_calls=pending_calls,
            pending_results=pending_results,
            next_tool_index=next_tool_index,
        )

    @staticmethod
    def _append_interaction(
        local: LocalMemoryManager,
        step: int,
        assistant_message: dict[str, Any],
        results: list[ToolResult],
    ) -> None:
        # 只有所有 ToolCall 都有 ToolResult 后，才原子写入 LocalMemory。
        local.append(
            interaction_id=f"step-{step}",
            assistant_message=assistant_message,
            tool_messages=[
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": result.model_dump_json(),
                }
                for result in results
            ],
        )

    @staticmethod
    def _parse_arguments(call: PendingModelToolCall) -> dict[str, Any] | ToolResult:
        try:
            arguments = json.loads(call.arguments_json)
        except json.JSONDecodeError as error:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                ok=False,
                error=ToolError(
                    code="invalid_arguments_json",
                    message="模型生成的工具参数不是合法 JSON",
                    details={"arguments": call.arguments_json, "error": str(error)},
                ),
            )
        if not isinstance(arguments, dict):
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                ok=False,
                error=ToolError(
                    code="invalid_arguments_json",
                    message="工具参数 JSON 顶层必须是 object",
                ),
            )
        return arguments

    @classmethod
    def _transition_context(
        cls,
        context: BaseContext,
        results: list[ToolResult],
    ) -> BaseContext:
        phase = cls._next_phase(context.working_memory.phase, results)
        if phase == context.working_memory.phase:
            return context
        return context.model_copy(
            update={
                "working_memory": context.working_memory.model_copy(
                    update={"phase": phase}
                )
            }
        )

    @staticmethod
    def _pause(checkpoint) -> AgentRunPause:
        status = {
            "awaiting_approval": "awaiting_approval",
            "recovery_required": "recovery_required",
            "awaiting_reconcile": "awaiting_reconcile",
        }.get(checkpoint.execution_state)
        if status is None:
            raise CheckpointConflictError(
                f"Checkpoint 状态不是可暂停状态：{checkpoint.execution_state}"
            )
        return AgentRunPause(
            status=status,
            checkpoint_id=checkpoint.checkpoint_id,
            revision=checkpoint.revision,
            approval_request=checkpoint.approval_request,
        )

    @staticmethod
    def _default_context(task: str) -> BaseContext:
        from tikiagent.context.models import WorkingMemory

        return BaseContext(
            agent="code_agent",
            working_memory=WorkingMemory(
                task=task,
                phase="execute",
                instruction=task,
            ),
        )
