"""结构化输出提取、校验和修正重试测试。"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from tikiagent.llm import (
    ModelSettings,
    OpenAICompatibleClient,
    StructuredOutputError,
)
from tikiagent.llm.structured_output import parse_structured_output


class Answer(BaseModel):
    value: int


class FakeCompletions:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        content = self.responses.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content)
                )
            ]
        )


def build_client(completions: FakeCompletions) -> OpenAICompatibleClient:
    sdk_client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return OpenAICompatibleClient(
        ModelSettings("key", "http://model.local/v1", "model"),
        client=sdk_client,
    )


def test_parses_json_from_markdown_fence() -> None:
    answer = parse_structured_output(
        '```json\n{"value": 7}\n```',
        Answer,
    )

    assert answer.value == 7


def test_client_returns_valid_structured_output() -> None:
    completions = FakeCompletions(['{"value": 9}'])
    client = build_client(completions)

    answer = client.complete_structured(
        messages=[{"role": "user", "content": "返回数字"}],
        response_type=Answer,
    )

    assert answer.value == 9
    assert "JSON Schema" in completions.requests[0]["messages"][0][
        "content"
    ]


def test_client_retries_once_after_validation_failure() -> None:
    completions = FakeCompletions(
        ['{"wrong": 1}', '{"value": 2}']
    )
    client = build_client(completions)

    answer = client.complete_structured(
        messages=[{"role": "user", "content": "返回数字"}],
        response_type=Answer,
    )

    assert answer.value == 2
    assert len(completions.requests) == 2
    assert "未通过结构校验" in completions.requests[1]["messages"][-1][
        "content"
    ]


def test_client_raises_after_two_invalid_outputs() -> None:
    client = build_client(FakeCompletions(["bad", "still bad"]))

    with pytest.raises(StructuredOutputError, match="连续两次"):
        client.complete_structured(
            messages=[{"role": "user", "content": "返回数字"}],
            response_type=Answer,
        )
