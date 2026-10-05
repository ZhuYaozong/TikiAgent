"""证据驱动的验证 Agent；模型提出判断，最终通过仍由程序计算。"""

import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from tikiagent.agents.research import ResearchAgent
from tikiagent.context.memory.local import LocalMemoryManager
from tikiagent.context.models import BaseContext, WorkingMemory
from tikiagent.context.preparation import ContextRuntime
from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.permissions.policy import RuleBasedPermissionPolicy
from tikiagent.harness.persistence.trace import _redact
from tikiagent.orchestration.contracts import ResearchResult, VerificationReport, VerificationCheck
from tikiagent.orchestration.requirements import CriterionAssessment, assessments_valid
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.models import ToolExecutionError, ToolError, ToolResult
from tikiagent.tools.registry import ToolRegistry, RegisteredTool
from tikiagent.runtime.lifecycle import final_context, complete_once
from tikiagent.harness.persistence.finalization import FinalizationLedger
from tikiagent.providers.llm.staged import at_stage
from tikiagent.runtime.diagnostics import finalization_error
from tikiagent.harness.persistence.budget import RequestBudgetExceeded
from tikiagent.runtime.guard import ToolLoopGuard
from tikiagent.providers.llm.openai_compatible import ModelOutputError
from tikiagent.context.preparation import ContextBudgetExceeded


class ReadEvidenceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    offset: int = Field(default=0, ge=0)


class SubmitVerificationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assessments: list[CriterionAssessment] = Field(min_length=1, max_length=12)
    recommendation: str = Field(min_length=1, max_length=1500)


class VerifierAgent:
    """独立上下文与工具注册表；没有 write/edit/run_command/调度权限。"""

    supports_harness = True
    supports_context = True

    def __init__(self, model, registry: ToolRegistry, *, context_runtime=None, observer=None,
                 max_steps: int = 6, max_tool_calls: int = 8, finalizations=None,
                 deduplicate=False, progress_revision=None):
        if min(max_steps, max_tool_calls) < 1:
            raise ValueError("验证取证预算必须为正数")
        self.model = model
        self.registry = registry
        self.context_runtime = context_runtime or ContextRuntime()
        self.observer = observer
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.finalizations = finalizations or FinalizationLedger()
        self.deduplicate, self.progress_revision = deduplicate, progress_revision

    def verify(self, *, handoff, result, specialist_results, execution_context, base_context=None):
        del specialist_results
        records = {}
        # 只有真实 Observation 关联的来源才可成为可引用证据，summary 只是被验证声明。
        if isinstance(result, ResearchResult):
            pairs = {(o.observation_id, url) for o in result.observations for url in o.urls}
            for index, source in enumerate(result.sources):
                records[f"source:{index}"] = {"usable": (source.observation_id, source.url) in pairs,
                                               "data": source.model_dump(mode="json")}
        else:
            for index, observation in enumerate(result.tool_results):
                records[f"execution:{index}"] = {"usable": self._usable(observation), "data": observation}

        registry = ToolRegistry()
        for name in self.registry.names():
            if name not in {"read_file", "list_files", "grep", "inspect_python_environment", "probe_python_import", "run_verification_tests"}:
                raise ValueError(f"Verifier Registry 不允许工具：{name}")
            registry.register(self.registry.get(name))
        submitted = None
        observed = set()
        evidence_cache = {}
        diagnosis = {}

        def read_evidence(evidence_id, offset=0):
            if evidence_id not in records:
                raise ToolExecutionError("unknown_evidence", "证据不存在")
            content = json.dumps(records[evidence_id], ensure_ascii=False)
            observed.add(evidence_id)
            return {"content": content[offset:offset + 5000], "next_offset": offset + 5000 if len(content) > offset + 5000 else None}

        def submit_verification(assessments, recommendation):
            nonlocal submitted
            checks = [CriterionAssessment.model_validate(item) for item in assessments]
            expected = {c.criterion_id for c in handoff.acceptance_criteria}
            if not expected or len(checks) != len(expected) or {c.criterion_id for c in checks} != expected:
                raise ToolExecutionError("acceptance_coverage", "必须逐项覆盖当前 Todo 的全部验收条件")
            if any(c.status == "passed" and not c.evidence_refs for c in checks):
                raise ToolExecutionError("missing_evidence_refs", "passed 必须填写结构化 evidence_refs；reason 中提及证据 ID 不能代替引用。请修正审核报告，不要重跑 Specialist")
            if any(ref not in records for c in checks for ref in c.evidence_refs):
                raise ToolExecutionError("unknown_evidence", "禁止引用不存在的证据")
            if any(ref not in observed for c in checks for ref in c.evidence_refs):
                raise ToolExecutionError("unread_evidence", "必须先读取引用证据，不能只根据 ID 猜测")
            if any(c.status == "passed" and any(not records[ref]["usable"] for ref in c.evidence_refs) for c in checks):
                raise ToolExecutionError("unusable_evidence", "失败、超时或不可用证据不能支持 passed；请如实提交 failed/insufficient_evidence，不要重复读取相同证据")
            submitted = checks, recommendation
            return {"submitted": True}

        registry.register(RegisteredTool("read_evidence", "分页读取当前 Result 或本轮验证产生的真实证据", ReadEvidenceArgs, read_evidence))
        registry.register(RegisteredTool("submit_verification", "逐条提交验收判断；passed 不由模型直接指定", SubmitVerificationArgs, submit_verification))
        final_registry = ToolRegistry()
        final_registry.register(registry.get("submit_verification"))
        harness = ExecutionHarness(Dispatcher(registry), permission_policy=RuleBasedPermissionPolicy(allowed_tools=registry.names()))
        context = base_context or BaseContext(agent="verifier", working_memory=WorkingMemory(task=handoff.instruction, phase="verification", instruction=handoff.instruction))
        # 将当前验收契约和声明放在硬上下文；完整执行输出按需读取，不整份复制进 Prompt。
        instruction = json.dumps({"instruction": handoff.instruction, "todo_id": handoff.todo_id,
            "handoff_id": handoff.handoff_id, "result_id": result.result_id,
            "criteria": [c.model_dump() for c in handoff.acceptance_criteria], "claim_excerpt": result.summary[:2000],
            "evidence_ids": list(records)}, ensure_ascii=False)
        context = context.model_copy(update={"working_memory": context.working_memory.model_copy(update={"instruction": instruction})})
        local = LocalMemoryManager()
        count = 0
        category = "model_response"
        reason = "Verifier 未提交结构化验收报告"
        step = 0
        identity = f"{execution_context.scope.session_id}/{execution_context.scope.task_id}/verifier/{result.result_id}"
        claimed = False
        while True:
            # 取证与提交分开计费：最多 max_steps 轮取证，再额外给一次只提交的机会。
            finalizing = count >= self.max_tool_calls or step >= self.max_steps
            if finalizing:
                claimed = self.finalizations.claim(identity, "verification_finalization")
                if not claimed:
                    reason = "验证收尾机会已消费，不自动重发未知请求"
                    break
            stage = "verification_finalization" if finalizing else "verification"
            budget_hint = json.dumps({"stage": stage,
                "remaining_evidence_calls": max(0, self.max_tool_calls - count),
                "remaining_evidence_rounds": max(0, self.max_steps - step),
                "observed_evidence": [{"evidence_id": ref, "usable": records[ref]["usable"]} for ref in sorted(observed)],
                "instruction": ("只根据已有证据单独提交报告，证据不足如实填写；同一证据可支持多条验收条件。" if finalizing else
                    "证据足够就单独提交报告，避免重复取证。") + "每条 passed 必须填写已读且可用的 evidence_refs，不能只在 reason 中写证据 ID。"}, ensure_ascii=False)
            context = context.model_copy(update={"working_memory": context.working_memory.model_copy(
                update={"phase": stage, "instruction": instruction + "\n验证运行状态：" + budget_hint})})
            try:
                prepare = (lambda **kwargs: final_context(self.context_runtime, **kwargs)) if finalizing else self.context_runtime.prepare
                prepared = prepare(base_context=context, local_memory=local.memory,
                    registry=final_registry if finalizing else registry)
                context = prepared.base_context
                local.replace(prepared.local_memory)
                response = (complete_once(at_stage(self.model, "verifier_final"), messages=prepared.messages, tool_schemas=prepared.tool_view.schemas)
                            if finalizing else self.model.complete(messages=prepared.messages, tool_schemas=prepared.tool_view.schemas))
            except (ModelOutputError, ContextBudgetExceeded, RequestBudgetExceeded) as error:
                diagnosis = finalization_error(error)
                self._emit("model_response", execution_context, handoff.handoff_id,
                           {"agent": "verifier", "stage": stage, "error_type": type(error).__name__,
                            **finalization_error(error)})
                reason = f"验证未完成：{type(error).__name__}"
                if finalizing:
                    break
                count = self.max_tool_calls
                continue
            except Exception as error:
                # 审核服务失败归属本轮审核，不向 Specialist 转嫁，也不伪造 PASS。
                diagnosis = finalization_error(error)
                reason = f"独立审核请求失败：{type(error).__name__}；尚未完成验收"
                self._emit("model_response", execution_context, handoff.handoff_id,
                           {"agent": "verifier", "stage": stage, **finalization_error(error)})
                break
            self._emit("model_response", execution_context, handoff.handoff_id, {"agent": "verifier", "stage": stage, **response.diagnostics})
            step += 1
            if finalizing:
                reason = "验证取证预算耗尽，最终提交机会未产生合法验收报告；尚未完成验收，不代表产物不合格"
            if not response.tool_calls:
                break
            messages = []
            for call in response.tool_calls:
                try:
                    args = json.loads(call.arguments_json)
                except ValueError:
                    args = None  # Basic Validation 返回结构化错误，不执行 handler。
                self._emit("tool_call_requested", execution_context, call.tool_call_id, {"tool_name": call.name, "agent": "verifier", "arguments": args})
                rejection = None
                revision = self.progress_revision() if self.progress_revision else "immutable"
                key = (ToolLoopGuard.fingerprint(call.name, call.arguments_json), revision)
                cached = evidence_cache.get(key) if self.deduplicate and call.name != "submit_verification" else None
                if call.name == "submit_verification" and len(response.tool_calls) != 1:
                    rejection = ("verification_submission_must_be_single", "提交报告必须单独调用，先观察全部取证结果")
                elif call.name != "submit_verification" and finalizing:
                    rejection = ("tool_not_exposed", "验证收尾阶段只允许 submit_verification，未执行取证工具")
                elif call.name != "submit_verification" and count >= self.max_tool_calls:
                    rejection = ("verification_evidence_budget_exceeded", "取证预算已耗尽，本项未执行；请根据已取得证据提交报告")
                if rejection:
                    # 批量中被拒绝的调用仍有对应 ToolResult，不伪造执行，不丢失消息配对。
                    observation = ToolResult(tool_call_id=call.tool_call_id, tool_name=call.name, ok=False,
                        error=ToolError(code=rejection[0], message=rejection[1]))
                elif cached is not None:
                    # 仅复用本轮Verifier亲自取得的证据，不用CodeAgent的PASS代替独立验证。
                    count += 1
                    observation = ToolResult.model_validate({k: v for k, v in cached.items() if k != "evidence_id"}).model_copy(
                        update={"tool_call_id": call.tool_call_id})
                else:
                    if call.name != "submit_verification":
                        count += 1
                    outcome = harness.handle({"tool_call_id": call.tool_call_id, "name": call.name, "arguments": args},
                        context=execution_context.model_copy(update={"agent": "verifier", "exposed_tools": prepared.tool_view.exposed_names}),
                        before_execute=lambda _call: self._emit("tool_execution_started", execution_context, call.tool_call_id, {"tool_name": call.name, "agent": "verifier"}))
                    observation = outcome.tool_result
                    if observation is None:
                        raise RuntimeError("Verifier 不支持挂起审批")
                self._emit("tool_result_received", execution_context, call.tool_call_id,
                           {"tool_name": call.name, "agent": "verifier", "tool_result": observation.model_dump(mode="json")})
                if cached is not None and not rejection:
                    content = {**cached, "tool_call_id": call.tool_call_id}
                elif not rejection and call.name not in {"submit_verification", "read_evidence"}:
                    evidence_id = f"verification:{uuid4()}"
                    records[evidence_id] = {"usable": self._usable(observation.model_dump()), "data": observation.model_dump(mode="json")}
                    observed.add(evidence_id)
                    content = {"evidence_id": evidence_id, **observation.model_dump(mode="json")}
                else:
                    content = observation.model_dump(mode="json")
                if call.name == "submit_verification" and not observation.ok:
                    category = "model_response"
                    reason = ("最终提交机会未产生合法报告：" if finalizing else "独立审核报告不合法：") + (observation.error.message if observation.error else "未知提交错误")
                if not rejection and observation.ok and call.name != "submit_verification":
                    evidence_cache[key] = content
                messages.append({"role": "tool", "tool_call_id": call.tool_call_id, "content": json.dumps(content, ensure_ascii=False)})
            # 即使恰好耗尽预算，也要先让本轮全部结果成为完整交互。
            local.append(interaction_id=f"verify-{step}", assistant_message=ResearchAgent._canonical_assistant_message(response), tool_messages=messages)
            if submitted or finalizing:
                break

        assessments, recommendation = submitted or ([], reason)
        if claimed:
            self.finalizations.finish(identity, "completed" if submitted else "failed",
                                      {} if submitted else diagnosis or {"error_category": "invalid_submission"})
        passed = assessments_valid(handoff.acceptance_criteria, assessments, records)
        if submitted:
            category = "insufficient_evidence" if any(c.status == "insufficient_evidence" for c in assessments) or not assessments else "validation"
            reason = "当前验收条件或有效证据未全部满足"
        # History 仅保存被引用的有界证据摘要；完整读取记录由 Trace 审计。
        selected = {ref for check in assessments for ref in check.evidence_refs}
        persisted = {ref: {"usable": records[ref]["usable"], "summary": json.dumps(_redact(records[ref]["data"], 1500), ensure_ascii=False)[:1500]}
                     for ref in selected if ref in records}
        return VerificationReport(result_id=result.result_id, handoff_id=handoff.handoff_id, todo_id=handoff.todo_id,
            subject_agent=handoff.to_agent, mode="agent", passed=passed, assessments=assessments,
            evidence_records=persisted, checks=[VerificationCheck(name=c.criterion_id, passed=c.status == "passed", evidence=c.reason) for c in assessments],
            failures=[] if passed else [reason], evidence=list(records), recommendation=recommendation,
            failure_category=None if passed else category, retryable=None if passed else category in {"validation", "insufficient_evidence"},
            blocking_reason=None if passed else reason,
            verification_status="assessed" if submitted else "not_performed", failure_scope="todo" if submitted else "run",
            allowed_actions=[] if passed else ["stop", "replan"] if submitted else ["stop"])

    @staticmethod
    def _usable(observation):
        output = observation.get("output")
        if observation.get("ok") is not True:
            return False
        if isinstance(output, dict):
            if output.get("timed_out") or ("exit_code" in output and output["exit_code"] != 0):
                return False
            if output.get("verified") is False:
                return False
        return True

    def _emit(self, event_type, context, correlation, data):
        if self.observer:
            self.observer(event_type, {"task_id": context.scope.task_id}, correlation, data)
