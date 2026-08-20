"""OpenAI-compatible 协议适配测试。"""

from types import SimpleNamespace

from tikiagent.llm import ModelSettings, OpenAICompatibleClient


class DumpableMessage:
    content = None
    tool_calls = [
        SimpleNamespace(
            id="call_001",
            function=SimpleNamespace(
                name="read_file",
                arguments='{"path":"demo.txt"}',
            ),
        )
    ]

    def model_dump(self, exclude_none: bool = True):
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_001",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"demo.txt"}',
                    },
                }
            ],
        }


class FakeCompletions:
    def __init__(self) -> None:
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=DumpableMessage())]
        )


def test_converts_internal_schema_and_normalizes_response() -> None:
    completions = FakeCompletions()
    sdk_client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    client = OpenAICompatibleClient(
        ModelSettings("test-key", "http://model.local/v1", "test-model"),
        client=sdk_client,
    )

    response = client.complete(
        messages=[{"role": "user", "content": "读取文件"}],
        tool_schemas=[
            {
                "name": "read_file",
                "description": "读取文件",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
    )

    assert completions.request["model"] == "test-model"
    assert completions.request["tools"][0]["type"] == "function"
    assert completions.request["tools"][0]["function"]["name"] == "read_file"
    assert response.tool_calls[0].tool_call_id == "call_001"
    assert response.tool_calls[0].arguments_json == '{"path":"demo.txt"}'
