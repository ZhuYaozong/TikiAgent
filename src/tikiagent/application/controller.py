"""ApplicationController：连接入口、Session、Chat 与 Workflow。"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from tikiagent.application.chat import ChatService
from tikiagent.application.events import EventBus
from tikiagent.application.models import (
    ApplicationOutcome,
    EventScope,
    ReconcileSubmission,
    ResponseRecord,
    TurnRecord,
    WorkflowOutcome,
)
from tikiagent.application.routing import IntentRouter
from tikiagent.application.session import SessionService
from tikiagent.application.workflow import WorkflowPort
from tikiagent.harness.recovery import RecoveryDecision


class ContextReferenceProvider(Protocol):
    def select(self, session_id: str) -> list[str]: ...


class ApplicationError(RuntimeError):
    """应用用例错误，不泄露 Graph 或 Harness 内部异常模型。"""


class ApplicationController:
    """只产生应用层事件；内部生命周期事件由 Adapter 转发。"""

    def __init__(
        self,
        *,
        sessions: SessionService,
        router: IntentRouter,
        chat: ChatService,
        workflow: WorkflowPort,
        context_refs: ContextReferenceProvider,
        event_bus: EventBus,
    ) -> None:
        self.sessions = sessions
        self.router = router
        self.chat = chat
        self.workflow = workflow
        self.context_refs = context_refs
        self.event_bus = event_bus

    def new_session(self, *, workspace_id: str) -> ApplicationOutcome:
        session = self.sessions.create(workspace_id=workspace_id)
        self.event_bus.emit(
            "session_started",
            scope=EventScope(
                session_id=session.session_id,
                workspace_id=session.workspace_id,
            ),
            source="application_controller",
            correlation_id=session.session_id,
            message="Session 已创建",
        )
        return ApplicationOutcome(
            status="session_created",
            session_id=session.session_id,
            message="Session 已创建",
        )

    def submit(self, *, session_id: str, user_input: str) -> ApplicationOutcome:
        session = self.sessions.sessions.load(session_id)
        has_history = bool(self.sessions.turns.list_records(session_id))
        decision = self.router.route(user_input, has_history=has_history)
        if decision.intent == "WORKFLOW" and session.active_checkpoint_id is not None:
            raise ApplicationError("当前 Session 有待恢复 Workflow，不能启动新任务")
        task_id = str(uuid4()) if decision.intent == "WORKFLOW" else None
        session, turn = self.sessions.record_turn(
            session_id=session_id,
            user_input=user_input,
            decision=decision,
            task_id=task_id,
        )
        scope = EventScope(
            session_id=session_id,
            workspace_id=session.workspace_id,
            turn_id=turn.turn_id,
            task_id=task_id,
        )
        received = self.event_bus.emit(
            "turn_received",
            scope=scope,
            source="application_controller",
            correlation_id=task_id or turn.turn_id,
            message=user_input,
        )
        routed = self.event_bus.emit(
            "intent_routed",
            scope=scope,
            source="application_controller",
            correlation_id=task_id or turn.turn_id,
            causation_id=received.event_id,
            message=f"Intent Router 返回 {decision.intent}",
            data={"reason": decision.reason},
        )

        if decision.intent == "CHAT":
            recent = self.sessions.turns.recent_messages(session_id)
            # 当前 Turn 已持久化；ChatService 会自行追加本轮 user_input，避免重复注入。
            if recent and recent[-1] == {"role": "user", "content": user_input}:
                recent = recent[:-1]
            content = self.chat.respond(
                user_input,
                recent_messages=recent,
            )
            outcome = ApplicationOutcome(
                status="chat_completed",
                session_id=session_id,
                turn_id=turn.turn_id,
                message=content,
            )
            self._persist_response(outcome)
            self._emit_final(outcome, causation_id=routed.event_id)
            return outcome

        assert task_id is not None
        workflow_outcome = self.workflow.start(
            task=user_input,
            scope=scope,
            context_refs=self.context_refs.select(session_id),
            causation_id=routed.event_id,
        )
        # 冻结顺序：Workflow 内 Checkpoint 成功落盘后，才能更新 Session 引用。
        self._synchronize_checkpoint(session_id, workflow_outcome)
        outcome = self._to_application(workflow_outcome, turn_id=turn.turn_id)
        self._persist_response(outcome)
        if outcome.status in {
            "workflow_completed",
            "workflow_denied",
            "workflow_failed",
        }:
            self._emit_final(outcome)
        return outcome

    def status(self, *, session_id: str) -> ApplicationOutcome:
        session = self.sessions.sessions.load(session_id)
        if session.active_checkpoint_id is None:
            return ApplicationOutcome(
                status="active",
                session_id=session_id,
                message="Session 当前没有待恢复 Workflow",
            )
        workflow = self.workflow.status(
            checkpoint_id=session.active_checkpoint_id,
            scope=EventScope(
                session_id=session_id,
                workspace_id=session.workspace_id,
            ),
        )
        return self._to_application(workflow)

    def resume(
        self,
        *,
        session_id: str,
        expected_revision: int,
        request_id: str,
        approved: bool,
    ) -> ApplicationOutcome:
        session = self._require_active(session_id)
        workflow = self.workflow.resume_approval(
            checkpoint_id=session.active_checkpoint_id,
            expected_revision=expected_revision,
            scope=self._session_scope(session),
            request_id=request_id,
            approved=approved,
        )
        return self._finish_resume(session_id, workflow)

    def recover(
        self,
        *,
        session_id: str,
        expected_revision: int,
        execution_id: str,
        action: str,
        decided_by: str,
        reason: str,
    ) -> ApplicationOutcome:
        session = self._require_active(session_id)
        decision = RecoveryDecision.model_validate(
            {"action": action, "decided_by": decided_by, "reason": reason}
        )
        workflow = self.workflow.recover(
            checkpoint_id=session.active_checkpoint_id,
            expected_revision=expected_revision,
            scope=self._session_scope(session),
            execution_id=execution_id,
            decision=decision,
        )
        return self._finish_resume(session_id, workflow)

    def reconcile(
        self,
        *,
        session_id: str,
        expected_revision: int,
        execution_id: str,
        submission: ReconcileSubmission,
    ) -> ApplicationOutcome:
        session = self._require_active(session_id)
        workflow = self.workflow.reconcile(
            checkpoint_id=session.active_checkpoint_id,
            expected_revision=expected_revision,
            scope=self._session_scope(session),
            execution_id=execution_id,
            submission=submission,
        )
        return self._finish_resume(session_id, workflow)

    def _finish_resume(
        self,
        session_id: str,
        workflow: WorkflowOutcome,
    ) -> ApplicationOutcome:
        self._synchronize_checkpoint(session_id, workflow)
        turn = self._workflow_turn(session_id, workflow.task_id)
        outcome = self._to_application(
            workflow,
            turn_id=turn.turn_id if turn else None,
        )
        self._persist_response(outcome)
        if outcome.status in {
            "workflow_completed",
            "workflow_denied",
            "workflow_failed",
        }:
            self._emit_final(outcome)
        return outcome

    def _synchronize_checkpoint(
        self,
        session_id: str,
        outcome: WorkflowOutcome,
    ) -> None:
        if outcome.status in {
            "awaiting_approval",
            "recovery_required",
            "awaiting_reconcile",
        }:
            if outcome.checkpoint_id is None:
                raise ApplicationError("暂停结果缺少权威 Checkpoint ID")
            self.sessions.bind_checkpoint(
                session_id=session_id,
                checkpoint_id=outcome.checkpoint_id,
            )
        else:
            session = self.sessions.sessions.load(session_id)
            if session.active_checkpoint_id is not None:
                self.sessions.clear_checkpoint(session_id=session_id)

    def _require_active(self, session_id: str):
        session = self.sessions.sessions.load(session_id)
        if session.active_checkpoint_id is None:
            raise ApplicationError("Session 没有活动 Checkpoint 引用")
        return session

    @staticmethod
    def _session_scope(session) -> EventScope:
        return EventScope(
            session_id=session.session_id,
            workspace_id=session.workspace_id,
            task_id=session.last_task_id,
            checkpoint_id=session.active_checkpoint_id,
        )

    def _workflow_turn(self, session_id: str, task_id: str) -> TurnRecord | None:
        for record in reversed(self.sessions.turns.list_records(session_id)):
            if isinstance(record, TurnRecord) and record.task_id == task_id:
                return record
        return None

    def _persist_response(self, outcome: ApplicationOutcome) -> None:
        if outcome.turn_id is None:
            return
        self.sessions.record_response(
            ResponseRecord(
                turn_id=outcome.turn_id,
                session_id=outcome.session_id,
                task_id=outcome.task_id,
                status=outcome.status,
                content=outcome.message,
                checkpoint_id=outcome.checkpoint_id,
            )
        )

    def _emit_final(
        self,
        outcome: ApplicationOutcome,
        *,
        causation_id: str | None = None,
    ) -> None:
        self.event_bus.emit(
            "final_answer",
            scope=EventScope(
                session_id=outcome.session_id,
                turn_id=outcome.turn_id,
                task_id=outcome.task_id,
                run_id=outcome.run_id,
            ),
            source="application_controller",
            correlation_id=outcome.task_id or outcome.session_id,
            causation_id=causation_id,
            message=outcome.message,
        )

    @staticmethod
    def _to_application(
        outcome: WorkflowOutcome,
        *,
        turn_id: str | None = None,
    ) -> ApplicationOutcome:
        status = {
            "awaiting_approval": "awaiting_approval",
            "recovery_required": "recovery_required",
            "awaiting_reconcile": "awaiting_reconcile",
            "completed": "workflow_completed",
            "denied": "workflow_denied",
            "failed": "workflow_failed",
        }[outcome.status]
        return ApplicationOutcome(
            status=status,
            session_id=outcome.session_id,
            turn_id=turn_id,
            task_id=outcome.task_id,
            run_id=outcome.run_id,
            checkpoint_id=outcome.checkpoint_id,
            checkpoint_revision=outcome.checkpoint_revision,
            approval_request_id=outcome.approval_request_id,
            execution_id=outcome.execution_id,
            attempt=outcome.attempt,
            tool_call_id=outcome.tool_call_id,
            tool_name=outcome.tool_name,
            tool_result=outcome.tool_result,
            message=outcome.final_result or outcome.message,
        )
