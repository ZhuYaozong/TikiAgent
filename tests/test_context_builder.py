"""ContextBuilder 的 Profile、Task Board 和 Base Context 测试。"""

from tikiagent.context.builder import ContextBuilder
from tikiagent.context.memory.history import InMemoryHistoryStore
from tikiagent.context.memory.models import HistoryRecord, NotepadEntry
from tikiagent.context.memory.notepad import InMemoryNotepadStore
from tikiagent.context.memory.retriever import Retriever
from tikiagent.context.models import ContextRequest, TaskBoard
from tikiagent.context.task_board import add_todo, start_todo


def board():
    value = add_todo(
        TaskBoard(),
        todo_id="research-1",
        owner="research_agent",
        description="research",
    )
    value = add_todo(
        value,
        todo_id="code-1",
        owner="code_agent",
        description="code page",
    )
    return add_todo(
        value,
        todo_id="code-2",
        owner="code_agent",
        description="test page",
    )


def request(agent: str, *, refs=None):
    return ContextRequest.model_validate(
        {
            "agent": agent,
            "task_id": "task-1",
            "session_id": "session-1",
            "phase": "execute",
            "instruction": "current instruction",
            "context_refs": refs or [],
        }
    )


def test_supervisor_sees_global_board_and_specialist_sees_own_todos() -> None:
    builder = ContextBuilder(Retriever(InMemoryHistoryStore()))
    task_board = board()

    supervisor = builder.build(
        request=request("supervisor"),
        task="hybrid",
        acceptance_criteria=["verified"],
        task_board=task_board,
    )
    code = builder.build(
        request=request("code_agent"),
        task="hybrid",
        acceptance_criteria=["verified"],
        task_board=task_board,
    )

    assert len(supervisor.working_memory.todos) == 3
    assert [item.todo_id for item in code.working_memory.todos] == [
        "code-1",
        "code-2",
    ]


def test_verifier_sees_todo_bound_to_result_ref() -> None:
    builder = ContextBuilder(Retriever(InMemoryHistoryStore()))
    task_board = start_todo(
        board(),
        todo_id="code-1",
        handoff_id="handoff-1",
    )
    item = task_board.items["code-1"].model_copy(
        update={"status": "awaiting_verification", "result_id": "result-1"}
    )
    task_board = task_board.model_copy(
        update={"items": task_board.items | {item.todo_id: item}}
    )

    context = builder.build(
        request=request("verifier", refs=["result-1"]),
        task="coding",
        acceptance_criteria=["tests pass"],
        task_board=task_board,
    )

    assert [todo.todo_id for todo in context.working_memory.todos] == ["code-1"]


def test_base_context_contains_history_but_no_react_messages() -> None:
    store = InMemoryHistoryStore()
    store.append(
        HistoryRecord(
            record_id="result-1",
            task_id="task-1",
            session_id="session-1",
            record_type="result",
            producer="research_agent",
            summary="research summary",
        )
    )
    context = ContextBuilder(Retriever(store)).build(
        request=request("code_agent", refs=["result-1"]),
        task="hybrid",
        acceptance_criteria=["verified"],
        task_board=board(),
    )

    dumped = context.model_dump()
    assert dumped["working_memory"]["relevant_history"][0]["record_id"] == (
        "result-1"
    )
    assert "messages" not in dumped
    assert "research summary" in context.render()


def test_context_builder_only_adds_relevant_approved_notepad() -> None:
    notes = InMemoryNotepadStore()
    notes.append(
        NotepadEntry(
            note_id="task-note",
            content="keep source URL",
            scope="task",
            task_id="task-1",
            source_refs=["result-1"],
            approved=True,
        )
    )
    notes.append(
        NotepadEntry(
            note_id="other-task",
            content="unrelated",
            scope="task",
            task_id="task-2",
            source_refs=["result-2"],
            approved=True,
        )
    )
    context = ContextBuilder(
        Retriever(InMemoryHistoryStore()),
        notepad_store=notes,
    ).build(
        request=request("code_agent"),
        task="hybrid",
        acceptance_criteria=["verified"],
        task_board=board(),
    )

    assert [
        item.note_id for item in context.working_memory.relevant_notepad
    ] == ["task-note"]
    assert "unrelated" not in context.render()
