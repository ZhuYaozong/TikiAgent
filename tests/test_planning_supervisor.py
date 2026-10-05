"""工具型 Supervisor 的真实 Graph、身份门控和恢复回归。"""

import json
from copy import deepcopy

from tikiagent.agents.planning import PlanningSupervisorAgent
from tikiagent.context.memory.history import InMemoryHistoryStore, JsonlHistoryStore
from tikiagent.context.memory.models import HistoryRecord
from tikiagent.harness.permissions.models import ApprovalDecision
from tikiagent.orchestration.contracts import CodeResult, ResearchResult, VerificationReport
from tikiagent.orchestration.workflow import MultiAgentWorkflow
from tikiagent.providers.llm.models import ModelResponse, ModelToolCall


def call(name, args, call_id=None):
    if name == "update_plan":
        for todo in args["todos"]:
            todo.setdefault("required_capabilities", ["web_research" if todo["owner"] == "research_agent" else "workspace_read"])
            todo.setdefault("acceptance_criteria", [{"criterion_id": "done", "description": "交付已核实"}])
    return ModelResponse(assistant_message={"role": "assistant"}, tool_calls=(
        ModelToolCall(call_id or name, name, json.dumps(args, ensure_ascii=False)),
    ))


def plan(*ids):
    return call("update_plan", {
        "goal": "完成两项文件任务", "acceptance_criteria": ["每项交付通过验证"],
        "todos": [{"todo_id": key, "description": f"交付 {key}", "owner": "code_agent",
                   "depends_on": list(ids[:index])} for index, key in enumerate(ids)],
    })


def delegate(key):
    return call("delegate_task", {"todo_id": key, "instruction": f"完成 {key}", "reason": "按任务依赖执行"}, f"delegate-{key}")


def stop():
    return call("stop_task", {"reason": "已有明确阻塞，停止重试"})


def review(key, action="accept", *, limitations=None, overrides=None):
    """测试模型读取真实委派 Observation，不能预先假定动态结果 ID。"""
    def response(messages):
        for message in reversed(messages):
            if message.get("role") != "tool":
                continue
            data = json.loads(message["content"])
            output = data.get("output") or {}
            if data.get("tool_name") == "delegate_task" and output.get("todo_id") == key:
                args = {"todo_id": key, "result_id": output["result_id"], "handoff_id": output["handoff_id"],
                    "verification_id": output["verification"]["verification_id"], "action": action,
                    "reason": "依据已取得交付作出明确验收决定", "limitations": limitations or []}
                return call("review_result", {**args, **(overrides or {})})
        raise AssertionError("没有找到对应 Todo 的返回观察")
    return response


class Script:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, *, messages, tool_schemas):
        self.requests.append(messages)
        response = self.responses.pop(0)
        return response(messages) if callable(response) else response


class Code:
    max_steps = 4

    def __init__(self):
        self.calls = []
        self.contexts = []

    def run(self, *, handoff, base_context):
        self.calls.append(handoff)
        self.contexts.append(base_context)
        return CodeResult(handoff_id=handoff.handoff_id, summary=f"已完成 {handoff.todo_id}", completed=True,
                          steps=1, changed_files=[f"{handoff.todo_id}.txt"])


class Gate:
    def __init__(self, passed=True):
        self.passed = passed

    def verify(self, *, handoff, raw_result, specialist_results):
        return VerificationReport(result_id=raw_result["result_id"], handoff_id=handoff.handoff_id,
                                  todo_id=handoff.todo_id,
                                  assessments=[{"criterion_id": c.criterion_id, "status": "passed" if self.passed else "failed", "evidence_refs": ["fixture"], "reason": "离线 Gate 夹具"} for c in handoff.acceptance_criteria],
                                  evidence_records={"fixture": {"usable": True}},
                                  subject_agent=handoff.to_agent, passed=self.passed, checks=[],
                                  failures=[] if self.passed else ["permission_denied: 所需能力不可用"],
                                  evidence=[], recommendation="finish" if self.passed else "stop")


def workflow(responses, *, gate=None, history=None, max_steps=32):
    model = Script(responses)
    agent = PlanningSupervisorAgent(model, max_steps=max_steps)
    code = Code()
    graph = MultiAgentWorkflow(supervisor=agent, research_agent=None, code_agent=code,
                               verification_gate=gate or Gate(), workspace_id="w", history_store=history)
    return model, code, graph


def test_same_agent_multiple_todos_keep_separate_verified_results():
    model, code, graph = workflow([plan("a", "b"), delegate("a"), review("a"), delegate("b"), review("b"), call("finish_task", {"reason": "都通过"})])
    result = graph.invoke("交付 a 和 b", session_id="s")
    assert result["status"] == "completed"
    assert [h.todo_id for h in code.calls] == ["a", "b"]
    assert len(result["results_by_id"]) == len(result["verifications_by_id"]) == 2
    assert "已完成 a" in result["final_result"] and "已完成 b" in result["final_result"]
    runtime = result["supervisor_runtime"]
    assert runtime["pending"] is None
    assert runtime["local_memory"] == {}  # 收尾只清理临时消息，保留 Result/Verification。
    # 返回 Supervisor 的观察包含验证证据，但没有子 Agent 的内部 messages。
    observation = json.loads(next(m["content"] for m in model.requests[-1] if m.get("tool_call_id") == "delegate-a"))
    assert observation["output"]["verification"]["passed"]
    assert "messages" not in observation["output"]


def test_failed_verification_allows_supervisor_to_stop_without_redelegation():
    model, code, graph = workflow([plan("a"), delegate("a"), stop()], gate=Gate(False))
    result = graph.invoke("执行任务")
    assert result["status"] == "stopped" and result["delegation_count"] == 1
    assert "permission_denied" in str(model.requests[-1])


def test_early_finish_is_rejected_and_returned_as_observation():
    model, code, graph = workflow([call("finish_task", {"reason": "直接结束"}), stop()])
    result = graph.invoke("尚未执行的任务")
    assert result["status"] == "stopped" and not code.calls
    assert "finish_not_verified" in str(model.requests[-1])


def test_dependency_cannot_be_bypassed():
    model, code, graph = workflow([plan("a", "b"), delegate("b"), stop()])
    result = graph.invoke("按依赖完成")
    assert not code.calls
    assert "dependency_incomplete" in str(model.requests[-1])


def test_agent_capability_mismatch_rejected_before_delegation():
    bad = call("update_plan", {"goal": "本地依赖检查", "acceptance_criteria": ["查明版本"],
        "todos": [{"todo_id": "env", "description": "查询 Python", "owner": "research_agent",
                   "required_capabilities": ["python_environment"]}]})
    model, code, graph = workflow([bad, stop()])
    graph.research_agent = object()
    result = graph.invoke("查询本地环境")
    assert not result["task_board"].items and not code.calls
    assert "capability_mismatch" in str(model.requests[-1])


def test_todo_criteria_cannot_be_rewritten_to_avoid_failure():
    initial = plan("a")
    args = json.loads(initial.tool_calls[0].arguments_json)
    args["todos"][0]["acceptance_criteria"][0]["description"] = "只需声称完成"
    model, _, graph = workflow([initial, call("update_plan", args), stop()])
    state = graph.invoke("文件任务")
    assert "immutable_acceptance" in str(model.requests[-1])
    assert state["task_board"].items["a"].acceptance_criteria[0].description == "交付已核实"


def test_replanning_cannot_delete_requirements():
    model, code, graph = workflow([plan("a", "b"), plan("a"), stop()])
    result = graph.invoke("两个任务")
    assert set(result["task_board"].items) == {"a", "b"}
    assert "plan_requirement_removed" in str(model.requests[-1])


def test_history_scope_and_repeated_failure_are_enforced():
    store = InMemoryHistoryStore()
    store.append(HistoryRecord(record_id="foreign", task_id="other", session_id="other",
                               record_type="result", producer="code_agent", summary="private"))
    responses = [call("read_history", {"record_id": "foreign", "offset": i}) for i in range(3)]
    model, code, graph = workflow(responses, history=store)
    result = graph.invoke("读取历史", session_id="s")
    assert result["status"] == "stopped" and "history_scope_denied" in result["final_result"]
    assert "private" not in str(model.requests)


def test_batch_with_delegate_is_rejected_before_any_mutation():
    first = plan("a")
    second = delegate("a")
    batch = ModelResponse(assistant_message={}, tool_calls=first.tool_calls + second.tool_calls)
    model, code, graph = workflow([batch, stop()])
    result = graph.invoke("任务")
    assert not result["task_board"].items and not code.calls
    assert "control_call_must_be_single" in str(model.requests[-1])


def test_supervisor_budget_is_persisted():
    _, _, graph = workflow([plan("a")], max_steps=1)
    result = graph.invoke("任务")
    assert result["status"] == "stopped"
    assert result["supervisor_runtime"]["steps"] == 1


def test_pending_supervisor_delegation_survives_approval_resume(tmp_path):
    from test_multi_agent_resume import build_workflow, ScriptedModel, first_response, final_response

    workspace, first = build_workflow(tmp_path, ScriptedModel([first_response()]),
                                     history_store=JsonlHistoryStore(tmp_path / "history.jsonl"))
    first.supervisor = PlanningSupervisorAgent(Script([plan("a"), delegate("a")]))
    workspace.resolve("input.txt").write_text("input", encoding="utf-8")
    paused = first.invoke("生成文件", session_id="s", task_id="t")
    assert paused["status"] == "awaiting_approval"
    cp = first.code_agent.agent.execution_coordinator.checkpoint_store.load(paused["runtime_checkpoint_id"])
    pending = cp.workflow_snapshot.state["supervisor_runtime"]["pending"]
    assert pending["handoff_id"] == paused["latest_handoff"].handoff_id
    assert len(cp.workflow_snapshot.state["supervisor_runtime"]["local_memory"]["recent_interactions"]) == 1
    _, second = build_workflow(tmp_path, ScriptedModel([final_response()]))
    second.verification_gate = Gate()
    model = Script([review("a"), call("finish_task", {"reason": "已验证"})])
    second.supervisor = PlanningSupervisorAgent(model)
    approval = cp.approval_request
    result = second.resume(cp.checkpoint_id, expected_revision=cp.revision,
                           approval_decision=ApprovalDecision(request_id=approval.request_id, approved=True,
                                                              scope=approval.scope, fingerprint=approval.fingerprint))
    assert result["status"] == "completed" and result["delegation_count"] == 1
    assert result["supervisor_runtime"]["pending"] is None
    assert result["supervisor_runtime"]["steps"] == 4
    assert "delegate-a" in str(model.requests[0])
    assert workspace.resolve("output.txt").read_text() == "graph resumed"


def test_pass_for_another_todo_cannot_finish_first_todo():
    _, _, graph = workflow([plan("a", "b"), delegate("a"), review("a"), delegate("b"), review("b"), call("finish_task", {"reason": "都通过"})])
    result = graph.invoke("两项工作")
    tampered = deepcopy(result)
    first, second = tampered["task_board"].items.values()
    tampered["verifications_by_id"][first.verification_id]["result_id"] = second.result_id
    unverified, incomplete = graph._finish_guard_failures(tampered)
    assert incomplete == [first.todo_id] and unverified == ["code_agent"]


def test_old_snapshot_migrates_identity_maps_without_trace():
    from tikiagent.orchestration.state import serialize_tiki_state, restore_tiki_state

    _, _, graph = workflow([plan("a"), delegate("a"), review("a"), call("finish_task", {"reason": "通过"})])
    result = graph.invoke("一项工作")
    payload = serialize_tiki_state(result)
    for key in ("supervisor_runtime", "results_by_id", "verifications_by_id"):
        payload.pop(key)
    for todo in payload["task_board"]["items"].values():
        todo.pop("depends_on")
    restored = restore_tiki_state(payload)
    assert restored["supervisor_runtime"] == {}
    assert len(restored["results_by_id"]) == 1
    assert graph._finish_guard_failures(restored) == ([], [])


def test_global_delegation_budget_is_not_reset_by_new_todo():
    model, code, graph = workflow([plan("a", "b"), delegate("a"), review("a"), delegate("b"), stop()])
    graph.max_delegations = 1
    result = graph.invoke("两项工作")
    assert result["delegation_count"] == 1 and len(code.calls) == 1
    assert "delegation_budget_exhausted" in str(model.requests[-1])


def test_hybrid_dependency_automatically_transfers_result_not_private_messages():
    class Research:
        def run(self, handoff, base_context):
            return ResearchResult(handoff_id=handoff.handoff_id, summary="调研结论：采用方案 A")

    setup = call("update_plan", {"goal": "调研后写页面", "acceptance_criteria": ["页面使用调研事实"],
        "todos": [{"todo_id": "research", "owner": "research_agent", "description": "调研"},
                  {"todo_id": "code", "owner": "code_agent", "description": "创建页面", "depends_on": ["research"]}]})
    model, code, graph = workflow([setup, delegate("research"), review("research"), delegate("code"), review("code"), call("finish_task", {"reason": "完成"})])
    graph.research_agent = Research()
    state = graph.invoke("调研后写页面")
    assert state["status"] == "completed"
    research = state["task_board"].items["research"]
    assert research.result_id in code.calls[0].context_refs
    assert research.verification_id in code.calls[0].context_refs
    assert "采用方案 A" in code.contexts[0].render()
