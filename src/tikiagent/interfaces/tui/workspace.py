"""只读 Workspace 目录快照；没有打开、编辑或删除 API。"""

from pathlib import Path
import os
import re

from tikiagent.interfaces.tui.models import WorkspaceEntry


class ReadOnlyWorkspaceSnapshotter:
    _SAFE_SESSION = re.compile(r"^[A-Za-z0-9._-]+$")

    def __init__(self, data_dir: str | Path, *, max_entries: int = 500) -> None:
        if max_entries < 1:
            raise ValueError("max_entries 必须大于 0")
        self.root = (Path(data_dir).resolve() / "workspaces").resolve()
        self.max_entries = max_entries

    def scan(self, session_id: str) -> tuple[WorkspaceEntry, ...]:
        if not self._SAFE_SESSION.fullmatch(session_id):
            raise ValueError("Session ID 包含不安全路径字符")
        session_root = (self.root / session_id).resolve()
        if not session_root.is_relative_to(self.root):
            raise ValueError("Workspace Snapshot 不能离开 workspaces 根目录")
        if not session_root.exists():
            return ()
        entries: list[WorkspaceEntry] = []
        for current, directories, files in os.walk(session_root, followlinks=False):
            directories.sort(key=str.casefold)
            files.sort(key=str.casefold)
            current_path = Path(current)
            safe_directories: list[str] = []
            for name in directories:
                child = current_path / name
                if child.is_symlink():
                    entries.append(self._entry(session_root, child, "symlink"))
                else:
                    entries.append(self._entry(session_root, child, "directory"))
                    safe_directories.append(name)
                if len(entries) >= self.max_entries:
                    return tuple(entries)
            directories[:] = safe_directories
            for name in files:
                child = current_path / name
                entries.append(self._entry(session_root, child, "symlink" if child.is_symlink() else "file"))
                if len(entries) >= self.max_entries:
                    return tuple(entries)
        return tuple(entries)

    @staticmethod
    def _entry(root: Path, path: Path, kind: str) -> WorkspaceEntry:
        relative = path.relative_to(root).as_posix()
        return WorkspaceEntry(path=relative, name=path.name, kind=kind, depth=len(Path(relative).parts) - 1)
