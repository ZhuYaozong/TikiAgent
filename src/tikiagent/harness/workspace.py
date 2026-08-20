"""文件工具的 Workspace 安全边界。"""

from pathlib import Path

from tikiagent.harness.models import ToolExecutionError


class Workspace:
    """把所有文件访问限制在一个明确的根目录内。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        raw_path = Path(relative_path)
        if raw_path.is_absolute():
            raise ToolExecutionError(
                code="workspace_escape",
                message="Workspace 不允许绝对路径",
                details={"path": relative_path},
            )

        target_path = (self.root / raw_path).resolve()
        try:
            target_path.relative_to(self.root)
        except ValueError as error:
            raise ToolExecutionError(
                code="workspace_escape",
                message="目标路径离开了 Workspace",
                details={"path": relative_path},
            ) from error

        return target_path

    def require_file(self, relative_path: str) -> Path:
        target_path = self.resolve(relative_path)
        if not target_path.exists():
            raise ToolExecutionError(
                code="file_not_found",
                message="文件不存在",
                details={"path": relative_path},
            )
        if not target_path.is_file():
            raise ToolExecutionError(
                code="not_a_file",
                message="目标路径不是文件",
                details={"path": relative_path},
            )
        return target_path

    def require_directory(self, relative_path: str) -> Path:
        target_path = self.resolve(relative_path)
        if not target_path.exists():
            raise ToolExecutionError(
                code="path_not_found",
                message="目录不存在",
                details={"path": relative_path},
            )
        if not target_path.is_dir():
            raise ToolExecutionError(
                code="not_a_directory",
                message="目标路径不是目录",
                details={"path": relative_path},
            )
        return target_path
