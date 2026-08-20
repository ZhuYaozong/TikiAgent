"""受 Workspace 约束的文件工具。"""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tikiagent.harness.models import ToolExecutionError
from tikiagent.harness.registry import RegisteredTool, ToolRegistry
from tikiagent.harness.workspace import Workspace


class StrictArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ReadFileArgs(StrictArgs):
    path: str


class WriteFileArgs(StrictArgs):
    path: str
    content: str


class EditFileArgs(StrictArgs):
    path: str
    old_text: str = Field(min_length=1)
    new_text: str


class ListFilesArgs(StrictArgs):
    path: str = "."
    recursive: bool = False


class GrepArgs(StrictArgs):
    pattern: str = Field(min_length=1)
    path: str = "."


def build_file_registry(workspace: Workspace) -> ToolRegistry:
    """创建绑定到指定 Workspace 的文件工具注册表。"""

    registry = ToolRegistry()

    def read_file(path: str) -> dict[str, Any]:
        target = workspace.require_file(path)
        return {"path": path, "content": target.read_text(encoding="utf-8")}

    def write_file(path: str, content: str) -> dict[str, Any]:
        target = workspace.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": path, "characters_written": len(content)}

    def edit_file(
        path: str,
        old_text: str,
        new_text: str,
    ) -> dict[str, Any]:
        target = workspace.require_file(path)
        original = target.read_text(encoding="utf-8")
        match_count = original.count(old_text)
        if match_count != 1:
            raise ToolExecutionError(
                code="edit_mismatch",
                message="old_text 必须在文件中唯一出现",
                details={"path": path, "match_count": match_count},
            )
        target.write_text(
            original.replace(old_text, new_text, 1),
            encoding="utf-8",
        )
        return {"path": path, "replacements": 1}

    def list_files(
        path: str = ".",
        recursive: bool = False,
    ) -> dict[str, Any]:
        target = workspace.require_directory(path)
        entries = target.rglob("*") if recursive else target.iterdir()
        files: list[str] = []
        for entry in sorted(entries, key=lambda item: item.as_posix()):
            display_path = entry.relative_to(workspace.root).as_posix()
            files.append(display_path + "/" if entry.is_dir() else display_path)
        return {"path": path, "files": files}

    def grep(pattern: str, path: str = ".") -> dict[str, Any]:
        target = workspace.resolve(path)
        if not target.exists():
            raise ToolExecutionError(
                code="path_not_found",
                message="搜索路径不存在",
                details={"path": path},
            )
        try:
            expression = re.compile(pattern)
        except re.error as error:
            raise ToolExecutionError(
                code="invalid_pattern",
                message="正则表达式不合法",
                details={"pattern": pattern, "error": str(error)},
            ) from error

        candidates = [target] if target.is_file() else [
            item for item in target.rglob("*") if item.is_file()
        ]
        matches: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                if expression.search(line):
                    matches.append(
                        {
                            "path": candidate.relative_to(
                                workspace.root
                            ).as_posix(),
                            "line": line_number,
                            "text": line,
                        }
                    )
        return {"pattern": pattern, "matches": matches}

    tools = (
        RegisteredTool(
            "read_file",
            "读取 Workspace 内的 UTF-8 文本文件",
            ReadFileArgs,
            read_file,
        ),
        RegisteredTool(
            "write_file",
            "向 Workspace 写入 UTF-8 文本文件",
            WriteFileArgs,
            write_file,
        ),
        RegisteredTool(
            "edit_file",
            "精确替换文件中唯一出现的一段文本",
            EditFileArgs,
            edit_file,
        ),
        RegisteredTool(
            "list_files",
            "列出 Workspace 内的文件和目录",
            ListFilesArgs,
            list_files,
        ),
        RegisteredTool(
            "grep",
            "使用正则表达式搜索 Workspace 内的文本",
            GrepArgs,
            grep,
        ),
    )
    for tool in tools:
        registry.register(tool)
    return registry
