"""仅虚构用户与静态资源；令牌是一次性测试凭据，不是生产身份认证。"""
from copy import deepcopy
import json
from uuid import uuid4

from mapping import CONTRACTS, command_hash

TOKENS = {"disposable-b05-alice": "alice", "disposable-b05-bob": "bob"}
CASES = json.loads((CONTRACTS / "fixtures/schema-cases.json").read_text(encoding="utf-8"))


def template(schema):
    return deepcopy(next(c["input"] for c in CASES if c["schema"] == schema and c["valid"]))


def command(principal="alice", mode="complete"):
    key = uuid4().hex
    payload = template("delegation-v1.schema.json")
    payload.update(principal_id=principal, delegation_id="del-" + key, platform_task_id="pt-" + key, run_id="run-" + key, instruction=mode)
    payload["resources"][0]["principal_id"] = principal
    payload["budget"].update(principal_id=principal, platform_task_id=payload["platform_task_id"], deadline="2099-01-01T00:00:00Z")
    value = {"schema_version": "1.0", "command_id": "cmd-" + key, "idempotency_key": key, "payload_hash": "", "identity": {"principal_id": principal, "issuer": "https://b05.invalid", "subject": principal, "actor": "tiki", "audience": "jobpilot", "scopes": ["resume.read"]}, "kind": "delegate", "platform_task_id": payload["platform_task_id"], "target_task_id": "domain-" + key, "sent_at": "2026-10-24T00:00:00Z", "payload": payload}
    return rehash(value)


def rehash(value):
    value["payload_hash"] = command_hash(value)
    return value


def response(original, request_id, response_type="text"):
    value = deepcopy(original)
    value.update(command_id="cmd-" + uuid4().hex, idempotency_key=uuid4().hex, kind="respond")
    value["payload"] = {"input_request_id": request_id, "expected_revision": 1, "response_id": "response-" + uuid4().hex, "response_type": response_type, "value": "confirmed"}
    return rehash(value)


def cancellation(original):
    value = deepcopy(original)
    value.update(command_id="cmd-" + uuid4().hex, idempotency_key=uuid4().hex, kind="cancel")
    value["payload"] = {"reason": "user request", "expected_revision": 1}
    return rehash(value)
