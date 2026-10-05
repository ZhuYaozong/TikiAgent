"""绑定调用阶段、输出预算和任务消费账本；不依赖共享可变模型配置。"""
from dataclasses import replace
from types import SimpleNamespace
from openai import APIConnectionError, APIStatusError
from tikiagent.runtime.policy import output_limit, thinking_effort
from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient


class StageModel:
    def __init__(self, root, stage, *, account=None, observer=None):
        self.root, self.stage = root, stage
        self.account, self.observer = account, observer

    def for_stage(self, stage):
        return StageModel(self.root, stage, account=self.account, observer=self.observer)

    def _client(self, final=False):
        original = self.root._get() if hasattr(self.root, "_get") else self.root
        if not isinstance(original, OpenAICompatibleClient):
            return original  # 离线夹具保留既有模型接口。
        sdk = original.client.with_options(max_retries=0) if hasattr(original.client, "with_options") else original.client
        def create(**kwargs):
            # SDK 隐式重试关闭；显式重试每一次均先扣费，收尾永不重试。
            for attempt in range(1 if final else original.settings.max_retries + 1):
                if self.account:
                    self.account.model_request(final=final)
                try:
                    return sdk.chat.completions.create(**kwargs)
                except (APIConnectionError, APIStatusError) as error:
                    transient = isinstance(error, APIConnectionError) or getattr(error, "status_code", 0) in {408, 429, 500, 502, 503, 504}
                    if final or not transient or attempt >= original.settings.max_retries:
                        raise
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        settings = replace(original.settings, max_output_tokens=output_limit(self.stage), safety_margin=2000,
                           reasoning_effort=thinking_effort(self.stage) if original.settings.api_style == "deepseek" else None)
        def observe(data):
            if self.observer:
                self.observer({"stage": self.stage, **data})
        return OpenAICompatibleClient(settings, client=client, observer=observe)

    def complete(self, messages, tool_schemas):
        return self._client().complete(messages=messages, tool_schemas=tool_schemas)

    def complete_structured(self, messages, response_type):
        return self._client().complete_structured(messages=messages, response_type=response_type)

    def complete_once(self, messages, tool_schemas):
        client = self._client(final=True)
        return getattr(client, "complete_once", client.complete)(messages=messages, tool_schemas=tool_schemas)

    def complete_structured_once(self, messages, response_type):
        client = self._client(final=True)
        return getattr(client, "complete_structured_once", client.complete_structured)(messages=messages, response_type=response_type)


def at_stage(model, stage):
    return model.for_stage(stage) if hasattr(model, "for_stage") else model
