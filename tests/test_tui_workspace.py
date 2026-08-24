from pathlib import Path

import pytest

from tikiagent.tui.workspace import ReadOnlyWorkspaceSnapshotter


def test_workspace_snapshot_is_scoped_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "workspaces" / "session-1"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('ok')", encoding="utf-8")
    snapshotter = ReadOnlyWorkspaceSnapshotter(tmp_path)

    entries = snapshotter.scan("session-1")

    assert [(item.path, item.kind) for item in entries] == [
        ("src", "directory"),
        ("src/app.py", "file"),
    ]
    assert not hasattr(snapshotter, "write")
    assert not hasattr(snapshotter, "delete")


def test_workspace_snapshot_rejects_path_like_session_id(tmp_path: Path) -> None:
    snapshotter = ReadOnlyWorkspaceSnapshotter(tmp_path)
    with pytest.raises(ValueError, match="不安全"):
        snapshotter.scan("../../outside")
