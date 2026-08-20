"""Workspace 边界测试。"""

from pathlib import Path

import pytest

from tikiagent.harness import ToolExecutionError, Workspace


def test_resolve_path_inside_workspace(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")

    target = workspace.resolve("notes/agent.txt")

    assert target == workspace.root / "notes" / "agent.txt"


@pytest.mark.parametrize("path", ["../outside.txt", "../../outside.txt"])
def test_rejects_workspace_escape(tmp_path: Path, path: str) -> None:
    workspace = Workspace(tmp_path / "workspace")

    with pytest.raises(ToolExecutionError) as caught:
        workspace.resolve(path)

    assert caught.value.code == "workspace_escape"


def test_rejects_absolute_path(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")

    with pytest.raises(ToolExecutionError) as caught:
        workspace.resolve(str((tmp_path / "outside.txt").resolve()))

    assert caught.value.code == "workspace_escape"
