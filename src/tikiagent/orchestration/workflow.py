"""Supervisor → Specialist → Verification Gate 的 Context-aware 工作流。"""

from collections.abc import Callable, Iterator, Mapping
from typing import Any, Literal, Protocol, cast

from langgraph.graph import END, START, StateGraph

from tikiagent.context.builder import ContextBuilder
from tikiagent.context.finalization import FinalizationService
from tikiagent.context.memory.history import (
    HistoryStore,
    InMemoryHistoryStore,
    JsonlHistoryStore,
)
from tikiagent.context.memory.models import HistoryRecord
from tikiagent.context.memory.notepad import InMemoryNotepadStore, NotepadStore
from tikiagent.context.memory.retriever import Retriever
from tikiagent.context.models import BaseContext, ContextProfile, ContextRequest
from tikiagent.context.schema import ContextAgentName
from tikiagent.context.task_board import (
    create_task_board,
    next_actionable_todo,
    record_result,
    record_verification,
    start_todo,
)
from tikiagent.harness.permissions.models import ApprovalDecision
from tikiagent.harness.persistence.checkpoint import (
    HistoryResumeReference,
    WorkflowResumeSnapshot,
)
from tikiagent.harness.persistence.recovery import ReconcileResult, RecoveryDecision
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.orchestration.completion import compose_final_answer
from tikiagent.orchestration.contracts import (
    CodeResult,
    Handoff,
    ResearchResult,
    SpecialistName,
    SupervisorDecision,
    SupervisorPlan,
    VerificationReport,
)
from tikiagent.orchestration.guards import latest_result_is_verified
from tikiagent.orchestration.requirements import report_satisfies, accepted_review_valid
from tikiagent.orchestration.state import (
    TikiState,
    create_multi_agent_state,
    restore_tiki_state,
    serialize_tiki_state,
)
from tikiagent.runtime.models import AgentRunPause


class Supervisor(Protocol):
    def plan(self, task: str) -> SupervisorPlan: ...

    def decide(
        self,
        state: TikiState,
        base_context: BaseContext,
    ) -> SupervisorDecision: ...


class ResearchSpecialist(Protocol):
    def run(
        self,
        handoff: Handoff,
        base_context: BaseContext,
    ) -> ResearchResult: ...


class CodeSpecialist(Protocol):
    max_steps: int

    def run(
        self,
        *,
        handoff: Handoff,
        base_context: BaseContext,
    ) -> CodeResult: ...


class ResultVerificationGate(Protocol):
    def verify(
        self,
        *,
        handoff: Handoff,
        raw_result: dict[str, Any],
        specialist_results: dict[str, dict[str, Any]],
    ) -> VerificationReport: ...


class MultiAgentWorkflow:
    """Graph Routing 主流程；Context Plane 通过依赖注入接入。"""

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        research_agent: ResearchSpecialist | None,
        code_agent: CodeSpecialist | None,
        verification_gate: ResultVerificationGate,
        workspace_id: str,
        max_delegations: int = 4,
        recursion_limit: int = 30,
        history_store: HistoryStore | None = None,
        notepad_store: NotepadStore | None = None,
        context_profiles: Mapping[
            ContextAgentName,
            ContextProfile,
        ]
        | None = None,
        artifact_guard=None,
    ) -> None:
        if max_delegations < 1:
            raise ValueError("max_delegations 必须大于 0")
        if recursion_limit < 1:
            raise ValueError("recursion_limit 必须大于 0")
        self.supervisor = supervisor
        self.research_agent = research_agent
        self.code_agent = code_agent
        self.verification_gate = verification_gate
        self.workspace_id = workspace_id
        self.max_delegations = max_delegations
        self.recursion_limit = recursion_limit
        self.max_steps = code_agent.max_steps if code_agent is not None else 1
        self.history_store = history_store or InMemoryHistoryStore()
        self.notepad_store = notepad_store or InMemoryNotepadStore()
        self.context_profiles = context_profiles
        self.artifact_guard = artifact_guard
        # 观察器只接收节点入口事实，不参与 Graph 路由或恢复决策。
        self.node_observer = None
        self._bind_context_services()
        self.graph = self._build_graph()

    def _bind_context_services(self) -> None:
        """切换持久化 History 后重建依赖它的 Context 服务。"""

        self.context_builder = ContextBuilder(
            Retriever(self.history_store),
            self.context_profiles,
            self.notepad_store,
        )
        self.finalization = FinalizationService(
            history_store=self.history_store,
            notepad_store=self.notepad_store,
        )

    def _supervisor_node(self, state: TikiState) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        decision_state = state
        tool_loop = getattr(self.supervisor, "supports_tool_loop", False)
        if tool_loop:
            decision, updates = self.supervisor.advance(
                state, history_store=self.history_store, context_builder=self.context_builder,
                finish_guard=self._finish_guard_failures,
                artifact_guard=self.artifact_guard,
                available_agents={name for name, agent in (("research_agent", self.research_agent), ("code_agent", self.code_agent)) if agent is not None},
            )
            decision_state = cast(TikiState, {**state, **updates})
        elif state["supervisor_plan"] is None:
            plan = self.supervisor.plan(state["task"])
            task_board = create_task_board(
                task=state["task"],
                owners=list(plan.required_specialists),
                code_task_mode=plan.code_task_mode,
            )
            updates = {
                "supervisor_plan": plan,
                "required_specialists": plan.required_specialists,
                "acceptance_criteria": plan.acceptance_criteria,
                "task_board": task_board,
                "recent_events": ["supervisor: plan"],
            }
            decision_state = cast(TikiState, {**state, **updates})

        if not tool_loop:
            base_context = self._build_context(
                state=decision_state, agent="supervisor", phase="routing",
                instruction="查看当前任务进度并决定下一条控制边。",
                context_refs=self._supervisor_context_refs(decision_state),
            )
            decision = self.supervisor.decide(decision_state, base_context)
        # 即便注入的 Supervisor 忽略预算，Graph 也不继续派发有风险的循环。
        if decision.action == "delegate" and (
            state["delegation_count"] >= state["max_delegations"]
            or (decision.target_agent == "code_agent" and (
                state.get("code_tool_call_count", 0) >= state.get("max_code_tool_calls", 60)
            ))
        ):
            decision = SupervisorDecision(
                action="stop", target_agent=None, instruction="",
                reason="Graph 执行保护：委派/工具预算耗尽或重复失败，请根据失败证据调整任务后重试",
            )
        if decision.action == "finish":
            unverified, incomplete_todos = self._finish_guard_failures(
                decision_state
            )
            if unverified or incomplete_todos:
                rejected = SupervisorDecision(
                    action="stop",
                    target_agent=None,
                    instruction="",
                    reason=(
                        "Graph 拒绝 FINISH：最新 Result 尚无有效验收 "
                        f"{unverified}；未完成 Todo {incomplete_todos}"
                    ),
                )
                return {
                    **updates,
                    "supervisor_decision": rejected,
                    "current_agent": "supervisor",
                    "status": "stopped",
                    "final_result": rejected.reason,
                    "recent_events": ["supervisor: finish rejected"],
                }
            return {
                **updates,
                "supervisor_decision": decision,
                "current_agent": "supervisor",
                "status": "finalizing",
                "final_result": self._completion_text(decision_state),
                "final_result_id": f"final:{decision_state['task_id']}",
                "recent_events": ["supervisor: finish guard passed"],
            }
        if decision.action == "stop":
            return {
                **updates,
                "supervisor_decision": decision,
                "current_agent": "supervisor",
                "status": "stopped",
                "final_result": decision.reason,
                "recent_events": ["supervisor: stop"],
            }

        target = decision.target_agent
        if target is None:
            raise RuntimeError("delegate 决策缺少 target_agent")
        self._require_agent_available(target)
        todo = (decision_state["task_board"].items.get(decision.todo_id) if decision.todo_id
                else next_actionable_todo(decision_state["task_board"], target))
        if todo is None:
            raise RuntimeError(f"{target} 没有可执行 Todo")
        if todo.owner != target:
            raise RuntimeError("委派目标与 Todo owner 不匹配")

        handoff = Handoff(
            from_agent="supervisor",
            to_agent=target,
            todo_id=todo.todo_id,
            delivery_mode=todo.delivery_mode,
            required_capabilities=todo.required_capabilities,
            acceptance_criteria=todo.acceptance_criteria,
            verification_level=todo.verification_level,
            verification_reason=todo.verification_reason,
            instruction=decision.instruction,
            # 应用显式选中的跨 Turn Result 必须随 Handoff 到达 Specialist；
            # Retriever 仍会强制同 Session 与 Profile record type 边界。
            context_refs=list(
                dict.fromkeys(
                    [*decision_state["session_context_refs"], *decision.context_refs]
                )
            ),
        )
        task_board = start_todo(
            decision_state["task_board"],
            todo_id=todo.todo_id,
            handoff_id=handoff.handoff_id,
        )
        if tool_loop:
            runtime = updates["supervisor_runtime"]
            pending = runtime.get("pending")
            if pending is None or pending["todo_id"] != todo.todo_id:
                raise RuntimeError("Supervisor 委派缺少匹配 Pending ToolCall")
            pending["handoff_id"] = handoff.handoff_id
        self._write_history(
            decision_state,
            record_id=handoff.handoff_id,
            record_type="handoff",
            producer="supervisor",
            summary=f"委派 {target}：{handoff.instruction}",
            payload=handoff.model_dump(mode="json"),
            refs=handoff.context_refs,
        )
        return {
            **updates,
            "supervisor_decision": decision,
            "current_agent": target,
            "latest_handoff": handoff,
            "task_board": task_board,
            "history_cursor": self.history_store.cursor(),
            "trace_cursor": self._runtime_trace_cursor(),
            "delegation_count": state["delegation_count"] + 1,
            "status": "delegating",
            "recent_events": [f"supervisor: delegate {target}"],
        }

    def _research_node(self, state: TikiState) -> dict[str, Any]:
        handoff = self._require_pending_handoff(state, "research_agent")
        if self.research_agent is None:  # pragma: no cover - 路由前已保护
            raise RuntimeError("ResearchAgent 未配置")
        base_context = self._build_context(
            state=state,
            agent="research_agent",
            phase="research",
            instruction=handoff.instruction,
            context_refs=[handoff.handoff_id, *handoff.context_refs],
            keywords=[handoff.instruction],
        )
        if getattr(self.research_agent, "supports_harness", False):
            result = self.research_agent.run(
                handoff,
                base_context,
                execution_context=ExecutionContext(
                    scope=ExecutionScope(
                        task_id=state["task_id"],
                        session_id=state["session_id"],
                        workspace_id=state["workspace_id"],
                    ),
                    agent="research_agent",
                    exposed_tools=set(),
                ),
            )
        else:
            result = self.research_agent.run(handoff, base_context)
        completed = handoff.model_copy(
            update={"result_id": result.result_id, "status": "completed"}
        )
        task_board = record_result(
            state["task_board"],
            todo_id=self._require_todo_id(handoff),
            handoff_id=handoff.handoff_id,
            result_id=result.result_id,
        )
        self._write_history(
            state,
            record_id=result.result_id,
            record_type="result",
            producer="research_agent",
            summary=result.summary,
            payload=result.model_dump(mode="json"),
            refs=[handoff.handoff_id, *handoff.context_refs],
        )
        return {
            "current_agent": "verification_gate",
            "latest_handoff": completed,
            "task_board": task_board,
            "history_cursor": self.history_store.cursor(),
            "trace_cursor": self._runtime_trace_cursor(),
            "specialist_results": {
                "research_agent": result.model_dump(mode="json")
            },
            "results_by_id": {**state.get("results_by_id", {}), result.result_id: result.model_dump(mode="json")},
            "status": "validating",
            "recent_events": ["research_agent: result"],
        }

    def _code_node(self, state: TikiState) -> dict[str, Any]:
        handoff = self._require_pending_handoff(state, "code_agent")
        if self.code_agent is None:  # pragma: no cover - 路由前已保护
            raise RuntimeError("CodeAgent 未配置")
        previous_report = state["specialist_verifications"].get("code_agent")
        phase = (
            "environment" if handoff.delivery_mode == "environment" else "inspection"
            if handoff.delivery_mode == "inspection"
            else "debugging"
            if previous_report is not None and not previous_report.passed
            else "coding"
        )
        base_context = self._build_context(
            state=state,
            agent="code_agent",
            phase=phase,
            instruction=handoff.instruction,
            context_refs=[handoff.handoff_id, *handoff.context_refs],
            keywords=[handoff.instruction],
        )
        if getattr(self.code_agent, "supports_resume", False):
            workflow_snapshot = self._workflow_snapshot(state)
            result = self.code_agent.run(
                handoff=handoff,
                base_context=base_context,
                execution_context=ExecutionContext(
                    scope=ExecutionScope(
                        task_id=state["task_id"],
                        session_id=state["session_id"],
                        workspace_id=state["workspace_id"],
                    ),
                    agent="code_agent",
                    # ContextRuntime 会在每次模型调用时写入真实 Tool View。
                    exposed_tools=set(),
                ),
                workflow_snapshot=workflow_snapshot,
            )
        else:
            result = self.code_agent.run(
                handoff=handoff,
                base_context=base_context,
            )
        if isinstance(result, AgentRunPause):
            return self._pause_updates(result)
        return self._record_code_result(state, handoff, result)

    def _record_code_result(
        self,
        state: TikiState,
        handoff: Handoff,
        result: CodeResult,
    ) -> dict[str, Any]:
        """正常执行与 Resume 共用同一条 Result/History 身份链。"""

        completed = handoff.model_copy(
            update={"result_id": result.result_id, "status": "completed"}
        )
        task_board = record_result(
            state["task_board"],
            todo_id=self._require_todo_id(handoff),
            handoff_id=handoff.handoff_id,
            result_id=result.result_id,
        )
        # ToolResult 仍是单次 ReAct 观察，不把可能很长的输出写入 History。
        history_payload = result.model_dump(
            mode="json",
            exclude={"tool_results"},
        )
        self._write_history(
            state,
            record_id=result.result_id,
            record_type="result",
            producer="code_agent",
            summary=result.summary,
            payload=history_payload,
            refs=[handoff.handoff_id, *handoff.context_refs],
        )
        return {
            "current_agent": "verification_gate",
            "code_tool_call_count": state.get("code_tool_call_count", 0) + len(result.tool_results),
            "latest_handoff": completed,
            "task_board": task_board,
            "history_cursor": self.history_store.cursor(),
            "specialist_results": {
                "code_agent": result.model_dump(mode="json")
            },
            "results_by_id": {**state.get("results_by_id", {}), result.result_id: history_payload},
            "trace_cursor": self._runtime_trace_cursor(),
            "status": "validating",
            "recent_events": ["code_agent: result"],
        }

    def _workflow_snapshot(self, state: TikiState) -> WorkflowResumeSnapshot:
        """Checkpoint 必须能在新进程中同时恢复 Graph 与 History。"""

        if not isinstance(self.history_store, JsonlHistoryStore):
            raise RuntimeError(
                "可恢复 CodeAgent 必须配置 JsonlHistoryStore，不能只保存 Agent Snapshot"
            )
        return WorkflowResumeSnapshot(
            state=serialize_tiki_state(state),
            history=HistoryResumeReference(
                path=str(self.history_store.path),
                cursor=self.history_store.cursor(),
            ),
        )

    def _pause_updates(self, pause: AgentRunPause) -> dict[str, Any]:
        """未完成 ToolCall 不伪造 ToolResult，也不进入 Verification。"""

        return {
            "current_agent": "code_agent",
            "status": pause.status,
            "runtime_checkpoint_id": pause.checkpoint_id,
            "runtime_checkpoint_revision": pause.revision,
            "trace_cursor": self._runtime_trace_cursor(),
            "resume_request": None,
            "recent_events": [f"code_agent: {pause.status}"],
        }

    def _runtime_trace_cursor(self) -> int:
        """返回正式 Code Runtime 已持久化的最新 Trace sequence。"""

        if self.code_agent is None or not getattr(
            self.code_agent,
            "supports_resume",
            False,
        ):
            return 0
        agent = getattr(self.code_agent, "agent")
        coordinator = getattr(agent, "execution_coordinator")
        events = coordinator.trace_store.list_events()
        return events[-1].sequence if events else 0

    def _resume_entry_node(self, state: TikiState) -> dict[str, Any]:
        """所有恢复动作在 Graph 内执行，不在 Graph 外手工续跑。"""

        request = state["resume_request"]
        if request is None:
            raise RuntimeError("Resume Entry 缺少 resume_request")
        handoff = self._require_pending_handoff(state, "code_agent")
        if self.code_agent is None or not getattr(
            self.code_agent,
            "supports_resume",
            False,
        ):
            raise RuntimeError("当前 CodeAgent 不支持 Resume")
        resume_method = getattr(self.code_agent, "resume")
        approval_raw = request.get("approval_decision")
        recovery_raw = request.get("recovery_decision")
        reconcile_raw = request.get("reconciliation")
        result = resume_method(
            handoff=handoff,
            checkpoint_id=request["checkpoint_id"],
            expected_revision=request["expected_revision"],
            approval_decision=(
                ApprovalDecision.model_validate(approval_raw)
                if approval_raw is not None
                else None
            ),
            recovery_decision=(
                RecoveryDecision.model_validate(recovery_raw)
                if recovery_raw is not None
                else None
            ),
            reconciliation=(
                ReconcileResult.model_validate(reconcile_raw)
                if reconcile_raw is not None
                else None
            ),
        )
        if isinstance(result, AgentRunPause):
            return self._pause_updates(result)
        return {
            **self._record_code_result(state, handoff, result),
            "resume_request": None,
            "runtime_checkpoint_id": None,
            "runtime_checkpoint_revision": None,
        }

    def _verification_node(self, state: TikiState) -> dict[str, Any]:
        handoff = state["latest_handoff"]
        if handoff is None or handoff.status != "completed":
            raise RuntimeError("Verification Gate 缺少 completed Handoff")
        raw_result = state["specialist_results"].get(handoff.to_agent)
        if raw_result is None:
            raise RuntimeError("Verification Gate 缺少 Specialist Result")

        # 规则验证不需要 Prompt；Agent 验证独立构建上下文，不继承子 Agent 消息。
        verification_arguments = {
            "handoff": handoff,
            "raw_result": raw_result,
            "specialist_results": state["specialist_results"],
        }
        needs_context = getattr(self.verification_gate, "needs_context", None)
        if (needs_context(handoff) if needs_context else getattr(self.verification_gate, "supports_context", False)):
            verification_arguments["base_context"] = self._build_context(
                state=state, agent="verifier", phase="verification",
                instruction=handoff.instruction,
                context_refs=[handoff.handoff_id, raw_result["result_id"], *handoff.context_refs],
                keywords=[handoff.instruction],
            )
        if (getattr(self.verification_gate, "supports_related_results", False)
                and getattr(self.supervisor, "supports_tool_loop", False)):
            # 多 Todo 下来源属于本次委派，不能误用同一 Agent 后来的另一份结果。
            verification_arguments["research_results"] = [
                record.payload for ref in handoff.context_refs
                if (record := self.history_store.get_by_id(ref)) is not None
                and record.record_type == "result" and record.producer == "research_agent"
                and record.session_id == state["session_id"]
            ]
        if getattr(self.verification_gate, "supports_harness", False):
            verification_arguments["execution_context"] = ExecutionContext(
                scope=ExecutionScope(
                    task_id=state["task_id"],
                    session_id=state["session_id"],
                    workspace_id=state["workspace_id"],
                ),
                agent="verifier",
                exposed_tools=set(),
            )
        report = self.verification_gate.verify(**verification_arguments)
        if getattr(self.supervisor, "supports_tool_loop", False):
            report = report.model_copy(update={"advisory": True})
        if (report.result_id != raw_result.get("result_id") or report.handoff_id != handoff.handoff_id
                or report.subject_agent != handoff.to_agent):
            raise RuntimeError("VerificationReport 与当前 Result/Handoff 身份不匹配")
        result_id = raw_result.get("result_id")
        if not isinstance(result_id, str):
            raise RuntimeError("Specialist Result 缺少 result_id")
        task_board = record_verification(
            state["task_board"],
            todo_id=self._require_todo_id(handoff),
            handoff_id=handoff.handoff_id,
            result_id=result_id,
            verification_id=report.verification_id,
            passed=report.passed,
            await_supervisor=state.get("requires_supervisor_review", False),
        )
        self._write_history(
            state,
            record_id=report.verification_id,
            record_type="verification",
            producer="verifier",
            summary=(
                f"{'基础检查' if report.verification_status == 'checks_only' else '审核'} {handoff.to_agent}："
                + ("未完成；" if report.verification_status == "not_performed" else
                   "机械边界满足；" if report.verification_status == "checks_only" and report.passed else
                   "基础检查阻断；" if report.verification_status == "checks_only" else
                   "符合条件；" if report.passed else "存在缺口；")
                + ("待 Supervisor 决定" if report.advisory else "旧规则工作流")
            ),
            payload=report.model_dump(mode="json"),
            refs=[result_id, handoff.handoff_id],
        )
        return {
            "current_agent": "supervisor",
            "verification_report": report,
            "specialist_verifications": {handoff.to_agent: report},
            "verifications_by_id": {**state.get("verifications_by_id", {}), report.verification_id: report.model_dump(mode="json")},
            "supervisor_runtime": (
                self.supervisor.complete_delegation(state, handoff, raw_result, report)
                if getattr(self.supervisor, "supports_tool_loop", False)
                else state.get("supervisor_runtime", {})
            ),
            "task_board": task_board,
            "history_cursor": self.history_store.cursor(),
            "status": "running",
            "recent_events": [
                f"verification_gate: {handoff.to_agent} passed={report.passed}"
            ],
        }

    def _finalization_node(self, state: TikiState) -> dict[str, Any]:
        """只有 FINISH Guard 通过后的状态才能进入幂等收尾。"""

        decision = state["supervisor_decision"]
        if (
            decision is None
            or decision.action != "finish"
            or state["status"] != "finalizing"
            or state["final_result"] is None
            or state["final_result_id"] is None
        ):
            raise RuntimeError("Finalization 缺少已通过 Guard 的 FINISH 状态")
        unverified, incomplete_todos = self._finish_guard_failures(state)
        if unverified or incomplete_todos:
            raise RuntimeError(
                "Finalization 拒绝执行：最新身份链或 TaskBoard 已失效"
            )
        refs = self._supervisor_context_refs(state)
        report = self.finalization.finalize(
            task_id=state["task_id"],
            session_id=state["session_id"],
            final_result_id=state["final_result_id"],
            final_result=state["final_result"],
            refs=refs,
        )
        # 已完成任务只保留统计和真实结果；私有消息与摘要缓存属于临时运行内存。
        runtime = dict(state.get("supervisor_runtime", {}))
        runtime.update(local_memory={}, history_summary_cache={}, pending=None)
        return {
            "current_agent": "supervisor",
            "status": "completed",
            "supervisor_runtime": runtime,
            "finalization_report": report,
            "history_cursor": self.history_store.cursor(),
            "recent_events": [
                "finalization: replay"
                if report.already_finalized
                else "finalization: persisted"
            ],
        }

    @staticmethod
    def _route_after_supervisor(
        state: TikiState,
    ) -> Literal["research", "code", "finalize", "end"]:
        decision = state["supervisor_decision"]
        if (
            decision is not None
            and decision.action == "finish"
            and state["status"] == "finalizing"
        ):
            return "finalize"
        if decision is None or decision.action != "delegate":
            return "end"
        if decision.target_agent == "research_agent":
            return "research"
        if decision.target_agent == "code_agent":
            return "code"
        raise RuntimeError("未知 target_agent")

    @staticmethod
    def _route_from_start(state: TikiState) -> Literal["normal", "resume"]:
        """Resume 只能通过 START → resume_entry 重新进入 Graph。"""

        return "resume" if state["resume_request"] is not None else "normal"

    @staticmethod
    def _route_after_code_or_resume(
        state: TikiState,
    ) -> Literal["verification", "end"]:
        if state["status"] in {
            "awaiting_approval",
            "recovery_required",
            "awaiting_reconcile",
        }:
            return "end"
        return "verification"

    def _build_graph(self):
        builder = StateGraph(TikiState)
        for name, handler in (("supervisor", self._supervisor_node),
                              ("research_agent", self._research_node),
                              ("code_agent", self._code_node),
                              ("resume_entry", self._resume_entry_node),
                              ("verification_gate", self._verification_node)):
            def observed(state, _name=name, _handler=handler):
                if self.node_observer is not None:
                    self.node_observer(_name, state)
                return _handler(state)
            builder.add_node(name, observed)
        builder.add_node("finalization", self._finalization_node)
        builder.add_conditional_edges(
            START,
            self._route_from_start,
            {"normal": "supervisor", "resume": "resume_entry"},
        )
        builder.add_conditional_edges(
            "supervisor",
            self._route_after_supervisor,
            {
                "research": "research_agent",
                "code": "code_agent",
                "finalize": "finalization",
                "end": END,
            },
        )
        builder.add_edge("research_agent", "verification_gate")
        builder.add_conditional_edges(
            "code_agent",
            self._route_after_code_or_resume,
            {"verification": "verification_gate", "end": END},
        )
        builder.add_conditional_edges(
            "resume_entry",
            self._route_after_code_or_resume,
            {"verification": "verification_gate", "end": END},
        )
        builder.add_edge("verification_gate", "supervisor")
        builder.add_edge("finalization", END)
        return builder.compile()

    def _build_context(
        self,
        *,
        state: TikiState,
        agent: ContextAgentName,
        phase: str,
        instruction: str,
        context_refs: list[str],
        keywords: list[str] | None = None,
    ) -> BaseContext:
        return self.context_builder.build(
            request=ContextRequest(
                agent=agent,
                task_id=state["task_id"],
                session_id=state["session_id"],
                phase=phase,
                instruction=instruction,
                context_refs=list(dict.fromkeys(context_refs)),
                keywords=keywords or [],
            ),
            task=state["task"],
            acceptance_criteria=state["acceptance_criteria"],
            task_board=state["task_board"],
        )

    def _write_history(
        self,
        state: TikiState,
        *,
        record_id: str,
        record_type: Literal["handoff", "result", "verification"],
        producer: ContextAgentName,
        summary: str,
        payload: dict[str, Any],
        refs: list[str],
    ) -> HistoryRecord:
        bounded_summary = (
            summary
            if len(summary) <= 8000
            else summary[:7980] + "...[summary truncated]"
        )
        return self.history_store.append(
            HistoryRecord(
                record_id=record_id,
                task_id=state["task_id"],
                session_id=state["session_id"],
                record_type=record_type,
                producer=producer,
                summary=bounded_summary,
                payload=payload,
                refs=list(dict.fromkeys(refs)),
            )
        )

    @staticmethod
    def _supervisor_context_refs(state: TikiState) -> list[str]:
        refs: list[str] = list(state["session_context_refs"])
        for todo in state["task_board"].items.values():
            refs.extend(ref for ref in (todo.result_id, todo.verification_id,
                todo.review.review_id if todo.review else None) if ref)
        handoff = state["latest_handoff"]
        if handoff is not None:
            refs.append(handoff.handoff_id)
        for result in state["specialist_results"].values():
            result_id = result.get("result_id")
            if isinstance(result_id, str):
                refs.append(result_id)
        for report in state["specialist_verifications"].values():
            refs.append(report.verification_id)
        return list(dict.fromkeys(refs))

    def _require_agent_available(self, target: SpecialistName) -> None:
        if target == "research_agent" and self.research_agent is None:
            raise RuntimeError("任务需要 ResearchAgent，但应用未配置")
        if target == "code_agent" and self.code_agent is None:
            raise RuntimeError("任务需要 CodeAgent，但应用未配置")

    @staticmethod
    def _require_pending_handoff(
        state: TikiState,
        expected_agent: SpecialistName,
    ) -> Handoff:
        handoff = state["latest_handoff"]
        if (
            handoff is None
            or handoff.status != "pending"
            or handoff.to_agent != expected_agent
        ):
            raise RuntimeError(f"{expected_agent} 缺少有效 pending Handoff")
        return handoff

    @staticmethod
    def _require_todo_id(handoff: Handoff) -> str:
        if handoff.todo_id is None:
            raise RuntimeError("Handoff 缺少 todo_id")
        return handoff.todo_id

    @staticmethod
    def _completion_text(state: TikiState) -> str:
        return compose_final_answer(state)

    def _finish_guard_failures(
        self,
        state: TikiState,
    ) -> tuple[list[SpecialistName], list[str]]:
        if state.get("requires_supervisor_review", False) and not state.get("results_by_id"):
            return list(state["required_specialists"]), list(state["task_board"].items)
        if state.get("results_by_id"):
            # 每个 Todo 校验自己的最新身份，不能用同一 Agent 后来的 PASS 覆盖前一项。
            incomplete = []
            unverified_owners = set()
            for item in state["task_board"].items.values():
                result = state["results_by_id"].get(item.result_id, {})
                report = state.get("verifications_by_id", {}).get(item.verification_id, {})
                if state.get("requires_supervisor_review", False):
                    invalid = not accepted_review_valid(item, result, report)
                    if self.artifact_guard:
                        invalid = invalid or bool(self.artifact_guard(item, result))
                    if invalid:
                        incomplete.append(item.todo_id)
                        unverified_owners.add(item.owner)
                    continue
                if (item.status != "completed" or not report.get("passed") or not report_satisfies(item, report)
                        or result.get("handoff_id") != item.handoff_id
                        or result.get("result_id") != item.result_id
                        or report.get("result_id") != item.result_id
                        or report.get("handoff_id") != item.handoff_id
                        or report.get("subject_agent") != item.owner):
                    incomplete.append(item.todo_id)
                    unverified_owners.add(item.owner)
            return sorted(unverified_owners), incomplete
        unverified = [
            specialist
            for specialist in state["required_specialists"]
            if not latest_result_is_verified(state, specialist)
        ]
        incomplete_todos = [
            item.todo_id
            for item in state["task_board"].items.values()
            if item.status != "completed"
        ]
        return unverified, incomplete_todos

    def initial_state(
        self,
        task: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        session_context_refs: list[str] | None = None,
    ) -> TikiState:
        state = create_multi_agent_state(
            task=task,
            workspace_id=self.workspace_id,
            max_steps=self.max_steps,
            max_delegations=self.max_delegations,
            session_id=session_id,
            task_id=task_id,
            session_context_refs=session_context_refs,
        )
        state["history_cursor"] = self.history_store.cursor()
        state["requires_supervisor_review"] = bool(getattr(self.supervisor, "supports_tool_loop", False))
        state["max_code_tool_calls"] = getattr(self, "task_code_tools", state["max_code_tool_calls"])
        return state

    def invoke(
        self,
        task: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        session_context_refs: list[str] | None = None,
    ) -> TikiState:
        result = self.graph.invoke(
            self.initial_state(
                task,
                session_id=session_id,
                task_id=task_id,
                session_context_refs=session_context_refs,
            ),
            config={"recursion_limit": self.recursion_limit},
        )
        return cast(TikiState, result)

    def resume(
        self,
        checkpoint_id: str,
        *,
        expected_revision: int,
        approval_decision: ApprovalDecision | None = None,
        recovery_decision: RecoveryDecision | None = None,
        reconciliation: ReconcileResult | None = None,
        snapshot_observer: Callable[[TikiState | None, TikiState], None] | None = None,
    ) -> TikiState:
        """加载权威快照，然后通过 START → Resume Entry 重新进入 Graph。"""

        if self.code_agent is None or not getattr(
            self.code_agent,
            "supports_resume",
            False,
        ):
            raise RuntimeError("当前工作流没有可恢复 CodeAgent")
        agent = getattr(self.code_agent, "agent")
        coordinator = getattr(agent, "execution_coordinator")
        checkpoint = coordinator.checkpoint_store.load(checkpoint_id)
        if checkpoint.revision != expected_revision:
            from tikiagent.harness.persistence.checkpoint import CheckpointConflictError

            raise CheckpointConflictError(
                f"Checkpoint revision 冲突：expected={expected_revision}, "
                f"actual={checkpoint.revision}"
            )
        if checkpoint.scope.workspace_id != self.workspace_id:
            raise RuntimeError("Checkpoint workspace scope 与当前 Workflow 不匹配")

        # History 通过稳定路径重新连接；不能从 Trace 或 Agent messages 猜历史。
        history_ref = checkpoint.workflow_snapshot.history
        restored_history = JsonlHistoryStore(history_ref.path)
        restored_history.require_cursor(history_ref.cursor)
        self.history_store = restored_history
        self._bind_context_services()

        state = restore_tiki_state(checkpoint.workflow_snapshot.state)
        if (
            state["task_id"] != checkpoint.scope.task_id
            or state["session_id"] != checkpoint.scope.session_id
            or state["workspace_id"] != checkpoint.scope.workspace_id
        ):
            raise RuntimeError("Workflow Snapshot 与 Execution Scope 身份不一致")
        state["resume_request"] = {
            "checkpoint_id": checkpoint_id,
            "expected_revision": expected_revision,
            "approval_decision": (
                approval_decision.model_dump(mode="json")
                if approval_decision is not None
                else None
            ),
            "recovery_decision": (
                recovery_decision.model_dump(mode="json")
                if recovery_decision is not None
                else None
            ),
            "reconciliation": (
                reconciliation.model_dump(mode="json")
                if reconciliation is not None
                else None
            ),
        }
        state["runtime_checkpoint_id"] = checkpoint_id
        state["runtime_checkpoint_revision"] = expected_revision
        snapshots = self.graph.stream(
            state,
            config={"recursion_limit": self.recursion_limit},
            stream_mode="values",
        )
        # 恢复同样逐节点发布事实；不能只展示恢复后的最终快照。
        previous = None
        result = state
        for snapshot in snapshots:
            result = cast(TikiState, snapshot)
            if snapshot_observer is not None:
                snapshot_observer(previous, result)
            previous = result
        return cast(TikiState, result)

    def stream(
        self,
        task: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        session_context_refs: list[str] | None = None,
    ) -> Iterator[TikiState]:
        snapshots = self.graph.stream(
            self.initial_state(
                task,
                session_id=session_id,
                task_id=task_id,
                session_context_refs=session_context_refs,
            ),
            config={"recursion_limit": self.recursion_limit},
            stream_mode="values",
        )
        for snapshot in snapshots:
            yield cast(TikiState, snapshot)

    def history_for(self, state: TikiState) -> list[HistoryRecord]:
        """返回当前任务可复用历史，供应用展示和测试。"""

        return self.history_store.list_records(
            task_id=state["task_id"],
            session_id=state["session_id"],
        )
