"""OpenAI-compatible 文本响应的结构化解析。"""

from typing import TypeVar

from pydantic import BaseModel, ValidationError


StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


class StructuredOutputError(ValueError):
    """模型文本无法转换为目标 Pydantic 模型。"""


def extract_json_object(content: str) -> str:
    """从纯 JSON 或 Markdown 代码块中提取最外层对象。"""

    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end < start:
        raise StructuredOutputError("模型没有返回 JSON 对象")
    return stripped[start : end + 1]


def parse_structured_output(
    content: str,
    response_type: type[StructuredModel],
) -> StructuredModel:
    """提取 JSON，并使用调用方指定的 Pydantic 模型校验。"""

    try:
        return response_type.model_validate_json(
            extract_json_object(content)
        )
    except ValidationError as error:
        raise StructuredOutputError(
            f"结构化输出校验失败：{error}"
        ) from error
