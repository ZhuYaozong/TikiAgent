"""TUI 斜杠命令解析；普通文本仍交给 Intent Router。"""

from typing import Literal
import shlex

from pydantic import BaseModel, ConfigDict


class TuiCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Literal["new", "session", "status", "approval", "recovery", "workspace", "help", "quit"]
    argument: str | None = None


def parse_command(value: str) -> TuiCommand | None:
    text = value.strip()
    if not text.startswith("/"):
        return None
    try:
        parts = shlex.split(text[1:])
    except ValueError as error:
        raise ValueError(f"命令引号不完整：{error}") from error
    if not parts:
        raise ValueError("请输入命令名称")
    name = parts[0].casefold()
    valid = {"new", "session", "status", "approval", "recovery", "workspace", "help", "quit"}
    if name not in valid:
        raise ValueError(f"未知 TUI 命令：/{name}")
    argument = " ".join(parts[1:]).strip() or None
    if name == "session" and argument is None:
        raise ValueError("/session 需要 Session ID")
    if name not in {"new", "session"} and argument is not None:
        raise ValueError(f"/{name} 不接受额外参数")
    return TuiCommand.model_validate({"name": name, "argument": argument})
