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
from tikiagent.tools.models import ToolExecutionError
from tikiagent.tools.registry import ToolRegistry, RegisteredTool


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
                 max_steps: int = 6, max_tool_calls: int = 8):
        self.model = model
        self.registry = registry
        self.context_runtime = context_runtime or ContextRuntime()
        self.observer = observer
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls

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
            if any(ref not in records for c in checks for ref in c.evidence_refs):
                raise ToolExecutionError("unknown_evidence", "禁止引用不存在的证据")
            if any(ref not in observed for c in checks for ref in c.evidence_refs):
                raise ToolExecutionError("unread_evidence", "必须先读取引用证据，不能只根据 ID 猜测")
            submitted = checks, recommendation
            return {"submitted": True}

        registry.register(RegisteredTool("read_evidence", "分页读取当前 Result 或本轮验证产生的真实证据", ReadEvidenceArgs, read_evidence))
        registry.register(RegisteredTool("submit_verification", "逐条提交验收判断；passed 不由模型直接指定", SubmitVerificationArgs, submit_verification))
        harness = ExecutionHarness(Dispatcher(registry), permission_policy=RuleBasedPermissionPolicy(allowed_tools=registry.names()))
        context = base_context or BaseContext(agent="verifier", working_memory=WorkingMemory(task=handoff.instruction, phase="verification", instruction=handoff.instruction))
        # 将当前验收契约和声明放在硬上下文；完整执行输出按需读取，不整份复制进 Prompt。
        instruction = json.dumps({"instruction": handoff.instruction, "todo_id": handoff.todo_id,
            "handoff_id": handoff.handoff_id, "result_id": result.result_id,
            "criteria": [c.model_dump() for c in handoff.acceptance_criteria], "claim": result.summary,
            "evidence_ids": list(records)}, ensure_ascii=False)
        context = context.model_copy(update={"working_memory": context.working_memory.model_copy(update={"instruction": instruction})})
        local = LocalMemoryManager()
        count = 0
        category = "budget"
        reason = "验证预算耗尽，未取得完整验收报告"
        for step in range(self.max_steps):
            prepared = self.context_runtime.prepare(base_context=context, local_memory=local.memory, registry=registry)
            context = prepared.base_context
            local.replace(prepared.local_memory)
            response = self.model.complete(messages=prepared.messages, tool_schemas=prepared.tool_view.schemas)
            self._emit("model_response", execution_context, handoff.handoff_id, {"agent": "verifier", **response.diagnostics})
            if not response.tool_calls:
                category, reason = "model_response", "Verifier 未提交结构化验收报告"
                break
            messages = []
            for call in response.tool_calls:
                if count >= self.max_tool_calls:
                    break
                count += 1
                try:
                    args = json.loads(call.arguments_json)
                except ValueError:
                    args = None  # Basic Validation 返回结构化错误，不执行 handler。
                if call.name == "submit_verification" and len(response.tool_calls) != 1:
                    # 模型必须真正观察取证结果之后，再单独提交报告。
                    args = None
                self._emit("tool_call_requested", execution_context, call.tool_call_id, {"tool_name": call.name, "agent": "verifier", "arguments": args})
                outcome = harness.handle({"tool_call_id": call.tool_call_id, "name": call.name, "arguments": args},
                    context=execution_context.model_copy(update={"agent": "verifier", "exposed_tools": prepared.tool_view.exposed_names}),
                    before_execute=lambda _call: self._emit("tool_execution_started", execution_context, call.tool_call_id, {"tool_name": call.name, "agent": "verifier"}))
                observation = outcome.tool_result
                if observation is None:
                    raise RuntimeError("Verifier 不支持挂起审批")
                self._emit("tool_result_received", execution_context, call.tool_call_id,
                           {"tool_name": call.name, "agent": "verifier", "tool_result": observation.model_dump(mode="json")})
                if call.name not in {"submit_verification", "read_evidence"}:
                    evidence_id = f"verification:{uuid4()}"
                    records[evidence_id] = {"usable": self._usable(observation.model_dump()), "data": observation.model_dump(mode="json")}
                    observed.add(evidence_id)
                    content = {"evidence_id": evidence_id, **observation.model_dump(mode="json")}
                else:
                    content = observation.model_dump(mode="json")
                messages.append({"role": "tool", "tool_call_id": call.tool_call_id, "content": json.dumps(content, ensure_ascii=False)})
                if submitted:
                    break
            if submitted or count >= self.max_tool_calls:
                break
            local.append(interaction_id=f"verify-{step}", assistant_message=ResearchAgent._canonical_assistant_message(response), tool_messages=messages)

        assessments, recommendation = submitted or ([], reason)
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
            blocking_reason=None if passed else reason)

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
