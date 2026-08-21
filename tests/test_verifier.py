"""只读 Verifier 的工具隔离和环境证据测试。"""

import sys
from pathlib import Path

import pytest

from tikiagent.agents import CommandCheck, EnvironmentVerifier
from tikiagent.harness import (
    Dispatcher,
    Workspace,
    build_read_only_file_registry,
    register_command_tool,
)
from tikiagent.orchestration import ActorResult, Plan


def build_verifier(
    tmp_path: Path,
    command: tuple[str, ...],
) -> tuple[Dispatcher, EnvironmentVerifier]:
    workspace = Workspace(tmp_path / "workspace")
    registry = build_read_only_file_registry(workspace)
    register_command_tool(registry, workspace)
    dispatcher = Dispatcher(registry)
    verifier = EnvironmentVerifier(
        dispatcher,
        (CommandCheck("tests", command),),
    )
    return dispatcher, verifier


def verify(verifier: EnvironmentVerifier):
    return verifier.verify(
        task="task",
        plan=Plan(
            goal="goal",
            steps=["run"],
            acceptance_criteria=["exit 0"],
        ),
        actor_result=ActorResult(
            completed=True,
            summary="done",
            steps=1,
        ),
    )


def test_verifier_registry_excludes_file_mutation_tools(
    tmp_path: Path,
) -> None:
    dispatcher, _ = build_verifier(
        tmp_path,
        (sys.executable, "-c", "print('ok')"),
    )

    assert dispatcher.registry.get("read_file") is not None
    assert dispatcher.registry.get("grep") is not None
    assert dispatcher.registry.get("write_file") is None
    assert dispatcher.registry.get("edit_file") is None


def test_nonzero_command_result_fails_verification(tmp_path: Path) -> None:
    _, verifier = build_verifier(
        tmp_path,
        (sys.executable, "-c", "raise SystemExit(3)"),
    )

    result = verify(verifier)

    assert result.passed is False
    assert result.checks[0].passed is False
    assert "exit_code': 3" in result.checks[0].evidence


def test_zero_exit_code_passes_verification(tmp_path: Path) -> None:
    _, verifier = build_verifier(
        tmp_path,
        (sys.executable, "-c", "print('ok')"),
    )

    result = verify(verifier)

    assert result.passed is True


def test_verifier_requires_at_least_one_check(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    dispatcher = Dispatcher(build_read_only_file_registry(workspace))

    with pytest.raises(ValueError, match="至少需要"):
        EnvironmentVerifier(dispatcher, ())
