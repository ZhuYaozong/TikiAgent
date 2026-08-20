"""Workspace 文件工具测试。"""

from pathlib import Path

from tikiagent.harness import Dispatcher, Workspace, build_file_registry


def setup_tools(tmp_path: Path) -> tuple[Workspace, Dispatcher]:
    workspace = Workspace(tmp_path / "workspace")
    dispatcher = Dispatcher(build_file_registry(workspace))
    return workspace, dispatcher


def dispatch(
    dispatcher: Dispatcher,
    name: str,
    arguments: dict[str, object],
):
    return dispatcher.dispatch(
        {
            "tool_call_id": f"call_{name}",
            "name": name,
            "arguments": arguments,
        }
    )


def test_write_read_edit_and_grep(tmp_path: Path) -> None:
    _, dispatcher = setup_tools(tmp_path)

    written = dispatch(
        dispatcher,
        "write_file",
        {"path": "calculator.py", "content": "return a - b\n"},
    )
    edited = dispatch(
        dispatcher,
        "edit_file",
        {
            "path": "calculator.py",
            "old_text": "return a - b",
            "new_text": "return a + b",
        },
    )
    read = dispatch(
        dispatcher,
        "read_file",
        {"path": "calculator.py"},
    )
    searched = dispatch(
        dispatcher,
        "grep",
        {"pattern": r"a \+ b", "path": "."},
    )

    assert written.ok is True
    assert edited.ok is True
    assert read.output["content"] == "return a + b\n"
    assert searched.output["matches"][0]["path"] == "calculator.py"


def test_edit_requires_exactly_one_match(tmp_path: Path) -> None:
    workspace, dispatcher = setup_tools(tmp_path)
    target = workspace.resolve("duplicate.txt")
    target.write_text("same\nsame\n", encoding="utf-8")

    result = dispatch(
        dispatcher,
        "edit_file",
        {
            "path": "duplicate.txt",
            "old_text": "same",
            "new_text": "changed",
        },
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "edit_mismatch"
    assert result.error.details["match_count"] == 2
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


def test_file_tool_cannot_escape_workspace(tmp_path: Path) -> None:
    _, dispatcher = setup_tools(tmp_path)

    result = dispatch(
        dispatcher,
        "write_file",
        {"path": "../../outside.txt", "content": "unsafe"},
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "workspace_escape"
