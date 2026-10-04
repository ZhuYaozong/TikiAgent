"""工具型 Supervisor：规划循环在此运行，委派交给 Graph 执行。"""

from copy import deepcopy
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tikiagent.context.memory.local import LocalMemoryManager
from tikiagent.context.memory.models import LocalMemory
from tikiagent.context.models import ContextRequest, TaskBoard, TodoItem
from tikiagent.context.preparation import ContextRuntime
from tikiagent.context.projections import verification_view
from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.permissions.policy import RuleBasedPermissionPolicy
from tikiagent.harness.scope import ExecutionContext, ExecutionScope
from tikiagent.orchestration.contracts import SupervisorDecision, SupervisorPlan
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.models import ToolError, ToolExecutionError, ToolResult
from tikiagent.tools.registry import RegisteredTool, ToolRegistry
from tikiagent.agents.capabilities import capability_prompt, supports
from tikiagent.orchestration.requirements import AcceptanceCriterion, Capability, DeliveryMode
from tikiagent.runtime.lifecycle import complete_once, final_context
from tikiagent.harness.persistence.finalization import FinalizationLedger
from tikiagent.providers.llm.openai_compatible import ModelOutputError
from tikiagent.context.preparation import ContextBudgetExceeded


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlannedTodo(Arguments):
    todo_id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    owner: Literal["research_agent", "code_agent"]
    delivery_mode: DeliveryMode = Field(default="artifact", description="文件交付用 artifact；只读文件调查用 inspection；依赖安装/环境操作用 environment，不要求凭空创建报告或测试")
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    required_capabilities: list[Capability] = Field(min_length=1, max_length=5)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=12)


class PlanArgs(Arguments):
    goal: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    todos: list[PlannedTodo] = Field(min_length=1, max_length=20)


class HistoryArgs(Arguments):
    record_id: str = Field(min_length=1)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=3000, ge=100, le=6000)


class DelegateArgs(Arguments):
    todo_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1, max_length=4000)
    context_refs: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=1000)


class EndArgs(Arguments):
    reason: str = Field(min_length=1, max_length=2000)


CONTROL_TOOLS = {"delegate_task", "finish_task", "stop_task"}


class PlanningSupervisorAgent:
    """每次运行到一次委派/结束就让出控制权；绝不在 handler 内嵌套 Graph。"""

    supports_tool_loop = True

    def __init__(self, model, *, context_runtime=None, max_steps=32, max_tool_calls=64, observer=None, finalizations=None):
        if min(max_steps, max_tool_calls) < 1:
            raise ValueError("Supervisor 预算必须为正数")
        self.model = model
        self.context_runtime = context_runtime or ContextRuntime()
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.observer = observer
        self.finalizations = finalizations or FinalizationLedger()

    def advance(self, state, *, history_store, context_builder, finish_guard, available_agents):
        working = dict(state)
        runtime = deepcopy(state.get("supervisor_runtime", {}))
        # 跨委派/审批恢复保留最严格上限，改变启动配置不能补充已消费预算。
        runtime["max_steps"] = min(self.max_steps, runtime.get("max_steps", self.max_steps))
        runtime["max_tool_calls"] = min(self.max_tool_calls, runtime.get("max_tool_calls", self.max_tool_calls))
        # 恢复时复用已经生成的 History 摘要，缓存仅属于当前 Workflow。
        engine = getattr(self.context_runtime.base_compressor, "engine", None)
        if engine is not None:
            engine.cache.update(runtime.get("history_summary_cache", {}))
            engine.calls = max(engine.calls, runtime.get("compression_calls", 0))
        local = LocalMemoryManager(LocalMemory.model_validate(runtime.get("local_memory", {})))
        if runtime.get("pending"):
            raise RuntimeError("Supervisor 待完成委派尚未取得 Verification，不能再次运行")
        chosen = None

        def fail(code, message):
            raise ToolExecutionError(code, message)

        def require_record(record_id):
            record = history_store.get_by_id(record_id)
            # 只允许当前 Session；跨任务引用必须由 Application 明确选入。
            if record is None or record.session_id != state["session_id"] or (
                record.task_id != state["task_id"] and record_id not in state["session_context_refs"]
            ) or record.record_type not in context_builder.profiles["supervisor"].allowed_record_types:
                fail("history_scope_denied", "历史记录不在当前任务或已授权的会话引用内")
            return record

        def read_history(record_id, offset=0, max_chars=3000):
            record = require_record(record_id)
            text = record.model_dump_json()
            return {"record_id": record_id, "content": text[offset:offset + max_chars],
                    "next_offset": offset + max_chars if offset + max_chars < len(text) else None}

        def update_plan(goal, acceptance_criteria, todos):
            old = working["task_board"]
            items = {}
            for raw in todos:
                item = PlannedTodo.model_validate(raw)
                if item.owner not in available_agents:
                    fail("agent_unavailable", "计划引用未配置的 Agent")
                if not supports(item.owner, item.required_capabilities):
                    fail("capability_mismatch", "Agent 不支持声明的能力；本地环境与文件调查交给 CodeAgent")
                ids = [criterion.criterion_id for criterion in item.acceptance_criteria]
                if len(ids) != len(set(ids)) or not any(c.required for c in item.acceptance_criteria):
                    fail("invalid_acceptance", "验收项 ID 必须唯一，至少一项为必需")
                if item.owner == "research_agent" and item.delivery_mode == "environment":
                    fail("capability_mismatch", "ResearchAgent 不能执行本地环境任务")
                if item.todo_id in items:
                    fail("invalid_plan", "Todo ID 重复")
                previous = old.items.get(item.todo_id)
                if previous is not None:
                    if previous.acceptance_criteria and previous.acceptance_criteria != item.acceptance_criteria:
                        fail("immutable_acceptance", "已有 Todo 验收项不能删除或放宽；失败需要补充证据或调整执行方式")
                    if previous.delivery_mode != item.delivery_mode:
                        fail("immutable_delivery_mode", "已有 Todo 的验收模式不可降级或改写")
                    if previous.status != "pending" and (
                        previous.description != item.description or previous.owner != item.owner
                        or previous.delivery_mode != item.delivery_mode or previous.depends_on != item.depends_on
                        or previous.required_capabilities != item.required_capabilities
                    ):
                        fail("immutable_todo", "已开始的 Todo 不得重写；请增加修复步骤")
                    # 已开始项保留全部真实状态，模型不能传入完成状态或结果身份。
                    items[item.todo_id] = TodoItem.model_validate({**previous.model_dump(), **item.model_dump()})
                else:
                    items[item.todo_id] = TodoItem.model_validate(item.model_dump())
            if not set(old.items) <= set(items):
                fail("plan_requirement_removed", "不能删除已有 Todo 来规避验收")
            for item in items.values():
                if item.todo_id in item.depends_on or not set(item.depends_on) <= set(items):
                    fail("invalid_dependency", "依赖必须引用其他已有 Todo")
            visited, active = set(), set()
            def visit(key):
                if key in active:
                    fail("dependency_cycle", "计划存在循环依赖")
                if key in visited:
                    return
                active.add(key)
                for dep in items[key].depends_on:
                    visit(dep)
                active.remove(key)
                visited.add(key)
            for key in items:
                visit(key)
            if working["supervisor_plan"] is not None and acceptance_criteria != working["acceptance_criteria"]:
                fail("immutable_acceptance", "原始验收标准不能由重新规划删除或改写")
            owners = list(dict.fromkeys(item.owner for item in items.values()))
            plan = SupervisorPlan(goal=goal, required_specialists=owners, acceptance_criteria=acceptance_criteria)
            working.update(supervisor_plan=plan, required_specialists=owners,
                           acceptance_criteria=acceptance_criteria, task_board=TaskBoard(items=items))
            runtime["plan_revision"] = runtime.get("plan_revision", 0) + 1
            return {"plan_revision": runtime["plan_revision"], "task_board": working["task_board"].model_dump(mode="json")}

        def delegate_task(todo_id, instruction, context_refs, reason):
            nonlocal chosen
            todo = working["task_board"].items.get(todo_id)
            if todo is None or todo.status not in {"pending", "failed"}:
                fail("todo_not_actionable", "Todo 不存在或不可执行")
            if not supports(todo.owner, todo.required_capabilities):
                fail("capability_mismatch", "当前 Todo 的能力不匹配")
            prior = working.get("verifications_by_id", {}).get(todo.verification_id, {})
            if prior.get("retryable") is False:
                previous = history_store.get_by_id(todo.handoff_id) if todo.handoff_id else None
                changed = previous is not None and previous.payload.get("instruction") != instruction
                if ("replan" not in prior.get("allowed_actions", []) or prior.get("failure_scope") != "run"
                        or not changed or todo.attempts >= 2):
                    fail("execution_blocked", "最新报告禁止原样重跑，或重规划尝试已耗尽：" + str(prior.get("blocking_reason") or prior.get("failure_category")))
            if any(working["task_board"].items[d].status != "completed" for d in todo.depends_on):
                fail("dependency_incomplete", "依赖 Todo 尚未完成")
            if state["delegation_count"] >= state["max_delegations"]:
                fail("delegation_budget_exhausted", "委派预算已耗尽，请停止")
            if todo.owner == "code_agent" and (
                state.get("code_tool_call_count", 0) >= state.get("max_code_tool_calls", 60)
            ):
                fail("execution_blocked", "CodeAgent 已触发执行保护，请停止或调整其他未完成任务")
            for ref in context_refs:
                require_record(ref)
            # 当前 Todo 的失败证据自动传递，模型不能因遗漏引用丢掉阻塞原因。
            dependency_refs = [
                ref for dependency in todo.depends_on
                for ref in (working["task_board"].items[dependency].result_id,
                            working["task_board"].items[dependency].verification_id) if ref
            ]
            refs = list(dict.fromkeys([*dependency_refs, *context_refs, *[r for r in (todo.result_id, todo.verification_id) if r]]))
            chosen = SupervisorDecision(action="delegate", target_agent=todo.owner, todo_id=todo_id,
                                        instruction=instruction, reason=reason, context_refs=refs)
            return {"scheduled": True, "todo_id": todo_id}

        def finish_task(reason):
            nonlocal chosen
            unverified, incomplete = finish_guard(working)
            if not working["task_board"].items or unverified or incomplete:
                fail("finish_not_verified", f"存在未完成或未验证任务：{incomplete}；{unverified}")
            chosen = SupervisorDecision(action="finish", target_agent=None, instruction="", reason=reason)
            return {"finish_guard_passed": True}

        def stop_task(reason):
            nonlocal chosen
            chosen = SupervisorDecision(action="stop", target_agent=None, instruction="", reason=reason)
            return {"stopped": True, "reason": reason}

        registry = ToolRegistry()
        for name, description, args, handler in [
            ("read_history", "读取作用域内 History 原文分页；记录 ID 来自当前上下文", HistoryArgs, read_history),
            ("update_plan", "创建或调整具体 Todo；保留已有 ID、原始验收和已执行事实", PlanArgs, update_plan),
            ("delegate_task", "按 todo_id 委派；必须单独调用，Graph 返回 Result 和 Verification 后再继续", DelegateArgs, delegate_task),
            ("finish_task", "请求完成；所有 Todo 最新结果必须有匹配 PASS；必须单独调用", EndArgs, finish_task),
            ("stop_task", "无法继续时说明具体阻塞原因并停止；必须单独调用", EndArgs, stop_task),
        ]:
            registry.register(RegisteredTool(name, description, args, handler))
        harness = ExecutionHarness(Dispatcher(registry), permission_policy=RuleBasedPermissionPolicy(allowed_tools=registry.names()))

        while (runtime.get("steps", 0) < runtime["max_steps"] and runtime.get("tool_calls", 0) < runtime["max_tool_calls"]
               and not runtime.get("force_finalization")):
            runtime["steps"] = runtime.get("steps", 0) + 1
            refs = list(dict.fromkeys([*[
                ref for todo in reversed(list(working["task_board"].items.values()))
                for ref in (todo.handoff_id, todo.result_id, todo.verification_id) if ref
            ], *working["session_context_refs"]]))
            context = context_builder.build(
                request=ContextRequest(agent="supervisor", task_id=state["task_id"], session_id=state["session_id"],
                                       phase="orchestration", instruction="观察最新事实，自主规划、委派、完成或停止。\n" + capability_prompt(available_agents), context_refs=refs),
                task=state["task"], acceptance_criteria=working["acceptance_criteria"], task_board=working["task_board"],
            )
            try:
                prepared = self.context_runtime.prepare(base_context=context, local_memory=local.memory, registry=registry)
                local.replace(prepared.local_memory)
                response = self.model.complete(messages=prepared.messages, tool_schemas=prepared.tool_view.schemas)
            except (ModelOutputError, ContextBudgetExceeded) as error:
                runtime["force_finalization"] = f"规划中断：{type(error).__name__}"
                self._emit("model_response", working, data={"agent": "supervisor", "error_type": type(error).__name__,
                                                            **getattr(error, "diagnostics", {})})
                break
            self._emit("model_response", working, data={"agent": "supervisor", **response.diagnostics})
            calls = response.tool_calls
            if not calls:
                chosen = SupervisorDecision(action="stop", target_agent=None, instruction="", reason="Supervisor 未请求编排工具：" + (response.final_text or "无输出"))
                break
            assistant = {"role": "assistant", "content": response.final_text, "tool_calls": [
                {"id": c.tool_call_id, "type": "function", "function": {"name": c.name, "arguments": c.arguments_json}} for c in calls
            ]}
            results = []
            invalid_batch = len(calls) > 1 and any(c.name in CONTROL_TOOLS for c in calls)
            for call in calls:
                runtime["tool_calls"] = runtime.get("tool_calls", 0) + 1
                self._emit("tool_call_requested", state, call.tool_call_id, {"tool_name": call.name})
                try:
                    if invalid_batch:
                        fail("control_call_must_be_single", "委派和结束操作必须独立调用，本批次未执行")
                    if runtime["tool_calls"] > runtime["max_tool_calls"]:
                        fail("supervisor_tool_budget", "Supervisor 工具预算已耗尽")
                    arguments = json.loads(call.arguments_json)
                    result = harness.handle(
                        {"tool_call_id": call.tool_call_id, "name": call.name, "arguments": arguments},
                        context=ExecutionContext(scope=ExecutionScope(task_id=state["task_id"], session_id=state["session_id"], workspace_id=state["workspace_id"]),
                                                 agent="supervisor", exposed_tools=prepared.tool_view.exposed_names),
                        before_execute=lambda _: self._emit("tool_execution_started", state, call.tool_call_id, {"tool_name": call.name}),
                    ).tool_result
                except (ValueError, ToolExecutionError) as error:
                    result = ToolResult(tool_call_id=call.tool_call_id, tool_name=call.name, ok=False,
                                        error=ToolError(code=getattr(error, "code", "invalid_arguments"), message=str(error)))
                if result is None:
                    raise RuntimeError("编排工具不得生成未处理的审批状态")
                if chosen is not None and chosen.action == "delegate" and result.ok:
                    # 未完成 ToolCall 保存在外层快照；等待 Graph 完成实际委派和验证。
                    runtime["pending"] = {"assistant_message": assistant, "tool_call_id": call.tool_call_id,
                                          "todo_id": chosen.todo_id, "handoff_id": None}
                    runtime["failures"] = {}
                    break
                self._emit("tool_result_received", state, call.tool_call_id, {"tool_name": call.name, "tool_result": result.model_dump(mode="json")})
                results.append({"role": "tool", "tool_call_id": call.tool_call_id, "content": result.model_dump_json()})
                if not result.ok:
                    key = hashlib.sha256((call.name + ":" + result.error.code).encode()).hexdigest()
                    failures = runtime.setdefault("failures", {})
                    failures[key] = failures.get(key, 0) + 1
                    if failures[key] >= 3:
                        runtime["force_finalization"] = f"编排连续同类失败：{call.name}/{result.error.code}；{result.error.message}"
                    if result.error.code == "delegation_budget_exhausted":
                        runtime["force_finalization"] = "委派预算已耗尽"
                else:
                    runtime["failures"] = {}
            if not runtime.get("pending"):
                local.append(interaction_id=f"supervisor-{runtime['steps']}", assistant_message=assistant, tool_messages=results)
            if chosen is not None:
                break
        if chosen is None:
            identity = f"{state['session_id']}/{state['task_id']}/supervisor"
            reason = runtime.get("force_finalization") or "Supervisor 达到规划步数或工具调用预算"
            runtime["finalization_consumed"] = True
            if self.finalizations.claim(identity, reason):
                outcome = "failed"
                try:
                    request = ContextRequest(agent="supervisor", task_id=state["task_id"], session_id=state["session_id"],
                        phase="orchestration", instruction="执行预算已耗尽；仅允许单独调用 finish_task 或 stop_task 总结已有事实，不得委派或修改计划。", context_refs=refs if 'refs' in locals() else [])
                    context = context_builder.build(request=request, task=state["task"],
                        acceptance_criteria=working["acceptance_criteria"], task_board=working["task_board"])
                    final_registry = ToolRegistry()
                    for name in ("finish_task", "stop_task"):
                        final_registry.register(registry.get(name))
                    prepared = final_context(self.context_runtime, base_context=context, local_memory=local.memory, registry=final_registry)
                    response = complete_once(self.model, messages=prepared.messages, tool_schemas=prepared.tool_view.schemas)
                    if len(response.tool_calls) == 1 and response.tool_calls[0].name in {"finish_task", "stop_task"}:
                        call = response.tool_calls[0]
                        args = EndArgs.model_validate(json.loads(call.arguments_json))
                        (finish_task if call.name == "finish_task" else stop_task)(args.reason)
                        outcome = "completed"
                except Exception:
                    pass  # 程序回退仍需准确保留停止，不重发收尾请求。
                self.finalizations.finish(identity, outcome)
            if chosen is None:
                chosen = SupervisorDecision(action="stop", target_agent=None, instruction="", reason=reason)
        runtime["local_memory"] = local.memory.model_dump(mode="json")
        if engine is not None:
            runtime["history_summary_cache"] = dict(engine.cache)
            runtime["compression_calls"] = engine.calls
        updates = {key: working[key] for key in ("supervisor_plan", "required_specialists", "acceptance_criteria", "task_board")}
        updates["supervisor_runtime"] = runtime
        return chosen, updates

    def complete_delegation(self, state, handoff, result, report):
        runtime = deepcopy(state.get("supervisor_runtime", {}))
        pending = runtime.get("pending")
        if not pending:
            return runtime
        if pending["todo_id"] != handoff.todo_id or pending["handoff_id"] != handoff.handoff_id:
            raise RuntimeError("Supervisor 待委派身份与返回结果不匹配")
        local = LocalMemoryManager(LocalMemory.model_validate(runtime.get("local_memory", {})))
        observation = ToolResult(tool_call_id=pending["tool_call_id"], tool_name="delegate_task", ok=True,
                                 output={"todo_id": handoff.todo_id, "handoff_id": handoff.handoff_id,
                                         "result_id": result["result_id"], "summary": result.get("summary", "")[:2000],
                                         "verification": verification_view(report.model_dump(mode="json"))})
        local.append(interaction_id=f"delegation-{handoff.handoff_id}", assistant_message=pending["assistant_message"],
                     tool_messages=[{"role": "tool", "tool_call_id": pending["tool_call_id"], "content": observation.model_dump_json()}])
        runtime.update(pending=None, local_memory=local.memory.model_dump(mode="json"))
        self._emit("tool_result_received", state, pending["tool_call_id"], {"tool_name": "delegate_task", "tool_result": observation.model_dump(mode="json")})
        return runtime

    def _emit(self, event_type, state, correlation_id=None, data=None):
        if self.observer:
            self.observer(event_type, state, correlation_id or state["task_id"], data or {})
