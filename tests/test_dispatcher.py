"""Dispatcher 固定验证规则测试。"""

from pathlib import Path

from tikiagent.harness import Dispatcher, Workspace, build_file_registry


def build_dispatcher(tmp_path: Path) -> Dispatcher:
    workspace = Workspace(tmp_path / "workspace")
    return Dispatcher(build_file_registry(workspace))


def test_unknown_tool_returns_structured_error(tmp_path: Path) -> None:
    dispatcher = build_dispatcher(tmp_path)

    result = dispatcher.dispatch(
        {
            "tool_call_id": "call_unknown",
            "name": "delete_everything",
            "arguments": {},
        }
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "unknown_tool"


def test_invalid_arguments_are_rejected_before_execution(tmp_path: Path) -> None:
    dispatcher = build_dispatcher(tmp_path)

    result = dispatcher.dispatch(
        {
            "tool_call_id": "call_invalid",
            "name": "write_file",
            "arguments": {"path": "answer.txt"},
        }
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_arguments"
    assert not (tmp_path / "workspace" / "answer.txt").exists()


def test_tool_execution_error_is_preserved(tmp_path: Path) -> None:
    dispatcher = build_dispatcher(tmp_path)

    result = dispatcher.dispatch(
        {
            "tool_call_id": "call_missing",
            "name": "read_file",
            "arguments": {"path": "missing.txt"},
        }
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "file_not_found"
    assert result.error.details == {"path": "missing.txt"}
