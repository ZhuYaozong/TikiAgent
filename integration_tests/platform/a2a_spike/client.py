"""官方 JSON-RPC transport，端点必须匹配本刀受控目录。"""
from uuid import uuid4
import base64
import json
from a2a.client.client import ClientCallContext
from google.protobuf.json_format import ParseDict
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import a2a_pb2 as p
import httpx

from mapping import EXT

BASE = "http://127.0.0.1:38446"


async def cancel(client, task_id, command):
    # CancelTask 无 metadata 字段，完整 B02 命令通过协商扩展的请求头传递。
    encoded = base64.urlsafe_b64encode(json.dumps(command, ensure_ascii=False).encode()).decode()
    return await client.cancel_task(p.CancelTaskRequest(id=task_id), context=ClientCallContext(service_parameters={"X-Tiki-Command": encoded}))


def request(command, task_id=""):
    return ParseDict({"message": {"messageId": uuid4().hex, "role": "ROLE_USER", "parts": [{"text": "B02 command envelope"}], "taskId": task_id}, "metadata": {EXT: {"version": "1.0", "command": command}}}, p.SendMessageRequest())


async def connect(principal="alice"):
    http = httpx.AsyncClient(headers={"Authorization": "Bearer disposable-b05-" + principal, "A2A-Extensions": EXT, "A2A-Version": "1.0"}, timeout=10, trust_env=False)
    try:
        response = await http.get(BASE + "/.well-known/agent-card.json")
        response.raise_for_status()
        card = ParseDict(response.json(), p.AgentCard())
        interface = card.supported_interfaces[0]
        if (interface.url, interface.protocol_binding, interface.protocol_version) != (BASE + "/rpc", "JSONRPC", "1.0"):
            raise ValueError("AgentCard 不匹配受控目录")
        if not any(e.uri == EXT and e.required for e in card.capabilities.extensions):
            raise ValueError("缺少必需扩展")
        return http, JsonRpcTransport(http, card, BASE + "/rpc")
    except BaseException:
        await http.aclose()
        raise
