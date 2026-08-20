"""Command Runtime 的结果语义与安全边界测试。"""

import sys
from pathlib import Path

from tikiagent.harness import (
    Dispatcher,
    Workspace,
    build_file_registry,
    register_command_tool,
)


def build_dispatcher(tmp_path: Path) -> Dispatcher:
    workspace = Workspace(tmp_path / "workspace")
    registry = build_file_registry(workspace)
    register_command_tool(registry, workspace)
    return Dispatcher(registry)


def run_command(
    dispatcher: Dispatcher,
    command: list[str],
    **arguments,
):
    return dispatcher.dispatch(
        {
            "tool_call_id": "call_command",
            "name": "run_command",
            "arguments": {"command": command, **arguments},
        }
    )


def test_successful_command_returns_exit_code_zero(tmp_path: Path) -> None:
    result = run_command(
        build_dispatcher(tmp_path),
        [sys.executable, "-c", "print(2 + 3)"],
    )

    assert result.ok is True
    assert result.output["exit_code"] == 0
    assert result.output["stdout"] == "5\n"
    assert result.output["timed_out"] is False


def test_nonzero_exit_is_a_command_result_not_tool_error(
    tmp_path: Path,
) -> None:
    result = run_command(
        build_dispatcher(tmp_path),
        [sys.executable, "-c", "raise SystemExit(7)"],
    )

    assert result.ok is True
    assert result.output["exit_code"] == 7
    assert result.error is None


def test_timeout_is_reported_without_losing_tool_result(tmp_path: Path) -> None:
    result = run_command(
        build_dispatcher(tmp_path),
        [sys.executable, "-c", "import time; time.sleep(1)"],
        timeout_seconds=0.05,
    )

    assert result.ok is True
    assert result.output["exit_code"] is None
    assert result.output["timed_out"] is True


def test_output_is_truncated_before_returning_to_agent(tmp_path: Path) -> None:
    result = run_command(
        build_dispatcher(tmp_path),
        [sys.executable, "-c", "print('x' * 1000)"],
        output_limit=100,
    )

    assert result.ok is True
    assert len(result.output["stdout"]) <= 100
    assert result.output["stdout_truncated"] is True
    assert result.output["stdout"].endswith("...[output truncated]")


def test_command_cwd_cannot_escape_workspace(tmp_path: Path) -> None:
    result = run_command(
        build_dispatcher(tmp_path),
        [sys.executable, "-c", "print('outside')"],
        cwd="../../",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "workspace_escape"


def test_missing_executable_is_a_structured_tool_error(tmp_path: Path) -> None:
    result = run_command(
        build_dispatcher(tmp_path),
        ["tikiagent-command-that-does-not-exist"],
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "command_not_found"
