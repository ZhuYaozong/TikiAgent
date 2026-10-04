"""长验证报告、失败 Turn、模型截断与文件链接的离线回归。"""

import json
from types import SimpleNamespace

import pytest

from tikiagent.application.models import IntentDecision, ResponseRecord
from tikiagent.context.builder import ContextBuilder
from tikiagent.context.compression.llm import LLMBaseCompressor, SummaryEngine
from tikiagent.context.memory.history import InMemoryHistoryStore
from tikiagent.context.memory.models import HistoryRecord, LocalMemory
from tikiagent.context.memory.retriever import Retriever
from tikiagent.context.models import ContextRequest, TaskBoard
from tikiagent.context.preparation import ContextRuntime
from tikiagent.context.projections import verification_view
from tikiagent.providers.llm.config import ModelSettings
from tikiagent.providers.llm.openai_compatible import ModelOutputError, OpenAICompatibleClient
from tikiagent.tools.registry import ToolRegistry
from test_application_controller import controller
from test_file_tools import setup_tools, dispatch
from test_llm_compression import Summarizer


def test_three_long_verifications_keep_control_facts_and_original_history():
    store = InMemoryHistoryStore()
    refs = []
    for index in range(3):
        record_id = f"verification-{index}"
        refs.append(record_id)
        store.append(HistoryRecord(
            record_id=record_id, task_id="task", session_id="session",
            record_type="verification", producer="verifier", summary="验证通过",
            payload={"verification_id": record_id, "result_id": f"result-{index}",
                     "handoff_id": f"handoff-{index}", "passed": True,
                     "assessments": [{"criterion_id": "c1", "status": "passed",
                                      "reason": "详细论证" * 3000, "evidence_refs": ["e1"]}],
                     "evidence_records": {"e1": {"usable": True, "content": "原始证据" * 10000}}},
        ))
    context = ContextBuilder(Retriever(store)).build(
        request=ContextRequest(agent="supervisor", task_id="task", session_id="session",
                               phase="orchestration", instruction="判断是否完成", context_refs=refs),
        task="生成空白页面", acceptance_criteria=["body 为空"], task_board=TaskBoard(),
    )
    assert len(context.render()) < 10000
    prepared = ContextRuntime().prepare(base_context=context, local_memory=LocalMemory(), registry=ToolRegistry())
    assert not prepared.usage.total_over_budget
    compressed = LLMBaseCompressor(SummaryEngine(Summarizer()), recent_records=0).compress(context)
    assert compressed.changed
    assert compressed.context.working_memory.control_facts == context.working_memory.control_facts
    assert compressed.context.working_memory.history_summary_refs == refs
    assert compressed.context.working_memory.acceptance_criteria == ["body 为空"]
    assert "原始证据" in store.get_by_id(refs[0]).payload["evidence_records"]["e1"]["content"]
    for fact in context.working_memory.control_facts:
        assert fact["assessments"] == [{"criterion_id": "c1", "status": "passed", "evidence_refs": ["e1"]}]


def test_delegation_projection_preserves_failure_identity_not_full_report():
    raw = {"verification_id": "v", "result_id": "r", "handoff_id": "h", "passed": False,
           "failure_category": "permission", "retryable": False, "blocking_reason": "禁止访问",
           "evidence_records": {"huge": "正文" * 10000}, "assessments": []}
    projected = verification_view(raw)
    assert projected["original_history_ref"] == "v"
    assert projected["blocking_reason"] == "禁止访问"
    assert projected["retryable"] is False
    assert "evidence_records" not in projected
    assert "evidence_records" in raw


def test_code_result_tool_bodies_are_indexed_not_duplicated():
    raw = HistoryRecord(record_id="r", task_id="t", session_id="s", record_type="result",
                        producer="code_agent", summary="结果", payload={"tool_results": [
                            {"tool_call_id": "call", "tool_name": "read_file", "ok": True,
                             "output": {"content": "正文" * 50000}}]})
    view = ContextBuilder._history_view(raw)
    assert len(view.model_dump_json()) < 1000
    assert view.payload["tool_results"][0]["evidence_id"] == "execution:0"
    assert "output" in raw.payload["tool_results"][0]


def test_workflow_exception_persists_failure_and_chat_does_not_replay(tmp_path):
    app, sessions, workflow, collector = controller(tmp_path)
    session_id = app.new_session(workspace_id="workspace").session_id
    def fail(**kwargs):
        raise RuntimeError("SECRET must not be persisted")
    workflow.start = fail
    outcome = app.submit(session_id=session_id, user_input="创建一个网页")
    assert outcome.status == "workflow_failed"
    records = sessions.turns.list_records(session_id)
    assert isinstance(records[-1], ResponseRecord)
    assert records[-1].turn_id == records[0].turn_id
    assert records[-1].error_stage == "workflow"
    assert "SECRET" not in records[-1].content
    assert collector.events[-1].data["error_category"] == "execution_error"
    seen = []
    app.chat.respond = lambda user_input, recent_messages: seen.append((user_input, recent_messages)) or "你好"
    assert app.submit(session_id=session_id, user_input="你好").status == "chat_completed"
    assert seen[0][0] == "你好"
    assert len(seen[0][1]) == 2
    assert "workflow_failed" in seen[0][1][-1]["content"]


def test_chat_failure_is_recorded_without_clearing_checkpoint(tmp_path):
    app, sessions, _, _ = controller(tmp_path)
    session_id = app.new_session(workspace_id="workspace").session_id
    sessions.bind_checkpoint(session_id=session_id, checkpoint_id="existing")
    def fail(*args, **kwargs):
        raise TimeoutError("secret")
    app.chat.respond = fail
    outcome = app.submit(session_id=session_id, user_input="你好")
    assert outcome.status == "chat_failed"
    assert sessions.sessions.load(session_id).active_checkpoint_id == "existing"
    assert sessions.turns.list_records(session_id)[-1].status == "chat_failed"


def test_recent_turns_pair_unknown_and_latest_resume_response(tmp_path):
    app, sessions, _, _ = controller(tmp_path)
    session_id = app.new_session(workspace_id="workspace").session_id
    _, turn = sessions.record_turn(session_id=session_id, user_input="旧任务",
                                  decision=IntentDecision(intent="WORKFLOW", reason="测试"), task_id="t")
    assert "状态未知" in sessions.turns.recent_messages(session_id)[-1]["content"]
    for status in ("awaiting_approval", "workflow_completed"):
        sessions.record_response(ResponseRecord(turn_id=turn.turn_id, session_id=session_id,
                                               status=status, content=status))
    messages = sessions.turns.recent_messages(session_id)
    assert len(messages) == 2 and messages[-1]["content"] == "workflow_completed"
    assert sessions.turns.recent_messages(session_id, limit=1) == []


class Message:
    def __init__(self, arguments='{"path":"safe.txt"}'):
        self.content = None
        self.tool_calls = [SimpleNamespace(id="call", function=SimpleNamespace(name="read_file", arguments=arguments))]

    def model_dump(self, **kwargs):
        return {"role": "assistant", "tool_calls": [{"id": "call", "type": "function",
                "function": {"name": "read_file", "arguments": self.tool_calls[0].function.arguments}}]}


@pytest.mark.parametrize("reason,arguments", [("length", '{"path":"safe.txt"}'), ("stop", '{"path":'), ("stop", 'null')])
def test_truncated_or_invalid_tool_arguments_never_escape_adapter(reason, arguments):
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason, message=Message(arguments))])
    client = OpenAICompatibleClient(ModelSettings("test", "http://invalid.local", "test"),
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(ModelOutputError):
        client.complete([], [])
    assert len(requests) == 2


def test_retry_accepts_complete_response_only():
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length" if len(requests) == 1 else "tool_calls", message=Message())])
    client = OpenAICompatibleClient(ModelSettings("test", "http://invalid.local", "test"),
        SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    response = client.complete([], [])
    assert len(requests) == 2
    assert json.loads(response.tool_calls[0].arguments_json) == {"path": "safe.txt"}


@pytest.mark.parametrize("tool,args", [("grep", {"pattern": "private"}), ("list_files", {"recursive": True})])
def test_recursive_tools_reject_external_symlink(tmp_path, tool, args):
    workspace, dispatcher = setup_tools(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    try:
        (workspace.root / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("系统不允许创建符号链接")
    result = dispatch(dispatcher, tool, args)
    assert not result.ok and result.error.code == "workspace_escape"


@pytest.mark.parametrize("tool,args", [("grep", {"pattern": "private"}), ("list_files", {"recursive": True})])
def test_recursive_tools_validate_each_child_before_read(tmp_path, monkeypatch, tool, args):
    from tikiagent.tools.models import ToolExecutionError

    workspace, dispatcher = setup_tools(tmp_path)
    (workspace.root / "child.txt").write_text("private", encoding="utf-8")
    original = workspace.resolve
    def resolve(path):
        if path == "child.txt":
            raise ToolExecutionError("workspace_escape", "模拟链接解析到外部")
        return original(path)
    # 无需 Windows 符号链接权限也能验证递归路径必须经过机械检查。
    monkeypatch.setattr(workspace, "resolve", resolve)
    result = dispatch(dispatcher, tool, args)
    assert result.error.code == "workspace_escape"
    assert result.output is None


def test_resume_model_failure_keeps_checkpoint_and_records_response(tmp_path):
    app, sessions, workflow, _ = controller(tmp_path)
    session_id = app.new_session(workspace_id="workspace").session_id
    pending = app.submit(session_id=session_id, user_input="创建网页")
    def fail(**kwargs):
        raise ModelOutputError("截断")
    workflow.resume_approval = fail
    outcome = app.resume(session_id=session_id, expected_revision=1, request_id="approval-1", approved=True)
    assert outcome.status == "workflow_failed"
    assert outcome.checkpoint_id == "cp-1"
    assert sessions.sessions.load(session_id).active_checkpoint_id == "cp-1"
    assert sessions.turns.list_records(session_id)[-1].turn_id == pending.turn_id
    assert sessions.turns.list_records(session_id)[-1].error_stage == "workflow_resume"


def test_cli_failure_outcome_has_nonzero_exit(tmp_path, monkeypatch, capsys):
    from tikiagent.interfaces import cli
    from tikiagent.application.models import ApplicationOutcome

    monkeypatch.setattr(cli, "execute", lambda args: ApplicationOutcome(
        status="workflow_failed", session_id="s", message="failed"))
    assert cli.main(["new-session", "--workspace-id", "w"]) == 1
    assert "workflow_failed" in capsys.readouterr().out


def test_adapter_emits_own_workflow_failure_without_secret(tmp_path):
    from tikiagent.application.workflow_adapter import TikiWorkflowAdapter
    from tikiagent.application.events import EventBus, CollectingEventSink
    from tikiagent.application.models import EventScope
    from tikiagent.harness.persistence.checkpoint import JsonCheckpointStore

    class BrokenWorkflow:
        def stream(self, *args, **kwargs):
            raise ModelOutputError("secret")
            yield  # 保持生成器接口，异常发生在 Graph 迭代时。
    bus = EventBus()
    sink = CollectingEventSink()
    bus.subscribe(sink)
    adapter = TikiWorkflowAdapter(workflow_factory=lambda *args: BrokenWorkflow(),
        checkpoint_store=JsonCheckpointStore(tmp_path / "checkpoints"), event_bus=bus)
    with pytest.raises(ModelOutputError):
        adapter.start(task="task", scope=EventScope(session_id="s", task_id="t", workspace_id="w"), context_refs=[])
    assert sink.events[-1].event_type == "workflow_failed"
    assert sink.events[-1].source == "workflow_adapter"
    assert "secret" not in sink.events[-1].message
