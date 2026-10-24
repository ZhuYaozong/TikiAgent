"""B02 命令与 A2A 应用扩展；本刀不修改旧 schema。"""
import hashlib
import json
from pathlib import Path

import rfc8785
from jsonschema import Draft202012Validator, FormatChecker
from a2a.utils.errors import InvalidParamsError

EXT = "https://contracts.tikiagent.invalid/a2a/binding/v1"
ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "docs/contracts"
VALIDATOR = Draft202012Validator(json.loads((CONTRACTS / "command-v1.schema.json").read_text(encoding="utf-8")), format_checker=FormatChecker())


def command_hash(command):
    identity = command["identity"]
    canonical = {key: identity[key] for key in ("principal_id", "actor", "audience")}
    canonical.update({key: command[key] for key in ("kind", "platform_task_id", "target_task_id", "payload")})
    return "sha256:" + hashlib.sha256(rfc8785.dumps(canonical)).hexdigest()


def validate(command, principal):
    if list(VALIDATOR.iter_errors(command)):
        raise InvalidParamsError("INVALID_COMMAND")
    identity = command["identity"]
    if identity != {"principal_id": principal, "issuer": "https://b05.invalid", "subject": principal, "actor": "tiki", "audience": "jobpilot", "scopes": ["resume.read"]}:
        raise InvalidParamsError("IDENTITY_MISMATCH")
    if command["payload_hash"] != command_hash(command):
        raise InvalidParamsError("HASH_MISMATCH")
    if command["kind"] == "delegate":
        payload = command["payload"]
        if payload["principal_id"] != principal or payload["platform_task_id"] != command["platform_task_id"]:
            raise InvalidParamsError("BINDING_MISMATCH")
        if payload["skill_id"] != "job.resume.read.v1":
            raise InvalidParamsError("SKILL_NOT_ALLOWED")
        if any(r["principal_id"] != principal for r in payload["resources"]):
            raise InvalidParamsError("RESOURCE_OWNER_MISMATCH")
        if payload["budget"]["principal_id"] != principal or payload["budget"]["platform_task_id"] != command["platform_task_id"]:
            raise InvalidParamsError("BUDGET_BINDING_MISMATCH")
    return (principal, identity["actor"], command["kind"], command["idempotency_key"])
