"""共享结构化请求渲染，预算检查和供应商调用使用同一份消息。"""

import json


def structured_messages(messages, schema):
    result = [dict(message) for message in messages]
    suffix = "\n只返回一个 JSON 对象，不要使用 Markdown。输出必须满足以下 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False)
    # 规则前置、格式要求后置；避免把不同 Schema 插到稳定规则之前。
    if result and result[0].get("role") == "system":
        if not result[0]["content"].endswith(suffix):
            result[0]["content"] += suffix
    else:
        result.insert(0, {"role": "system", "content": suffix})
    return result
