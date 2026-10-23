"""离线契约验证与规范场景参考实现，不是生产鉴权或任务执行器。"""
import json
import sys
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=(ValueError, TypeError))
def utc_timestamp(value):
    """显式检查真实日期，不依赖 jsonschema 可选日期格式依赖。"""
    if not isinstance(value, str):
        return True
    return value.endswith("Z") and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None


def unique_object(pairs):
    """拒绝重复 JSON 属性，避免两个语言解析出不同授权参数。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 属性: {key}")
        result[key] = value
    return result


def read(path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def local_references(node):
    """契约只能引用本文件定义，禁止校验时访问远程地址。"""
    if isinstance(node, dict):
        if "$ref" in node and not node["$ref"].startswith("#/$defs/"):
            raise ValueError("非本地引用")
        for value in node.values():
            local_references(value)
    elif isinstance(node, list):
        for value in node:
            local_references(value)


def semantic(rule, x, policy):
    """只验证给定事实下的契约决策；认证与事务事实由未来服务提供。"""
    if rule == "transition":
        if policy["owners"].get(x["kind"]) != x["writer"]:
            return "FORBIDDEN"
        if x["revision"] != x["expected_revision"]:
            return "REVISION_CONFLICT"
        if not policy["transitions"][x["before"]]:
            return "ALREADY_TERMINAL"
        if x["effect_status"] == "unknown" and x["after"] in {"COMPLETED", "FAILED", "CANCELED", "REJECTED"}:
            return "RECONCILE"
        return "APPLY" if x["after"] in policy["transitions"][x["before"]] else "INVALID_TRANSITION"
    if rule == "idempotency":
        old, new = x["stored"], x["incoming"]
        if any(old[key] != new[key] for key in ("principal", "caller", "kind", "key")):
            return "NEW"
        return "DUPLICATE" if old["hash"] == new["hash"] else "IDEMPOTENCY_CONFLICT"
    if rule == "projection":
        event, snap = x["event"], x["event"]["snapshot"]
        if any(v != x["expected_principal"] for v in (event["principal_id"], snap["principal_id"])):
            return "FORBIDDEN"
        if (event["source"] != snap["owner"] or event["source_task_id"] != snap["task_id"]
                or event["source_revision"] != snap["revision"]
                or event["platform_task_id"] != x["expected_platform_task"]
                or snap["platform_task_id"] != x["expected_platform_task"]
                or snap["run_id"] != x["root_run_id"]
                or (snap["task_kind"] == "runtime" and snap["task_id"] != x["root_run_id"])):
            return "INVALID_BINDING"
        key = event["source"] + "/" + event["source_task_id"]
        if event["source"] + "/" + event["event_id"] in x["seen_event_ids"] or event["source_revision"] <= x["source_revisions"].get(key, 0):
            return "IGNORE"
        return "APPLY_ROOT" if snap["task_kind"] == "runtime" else "APPLY_CHILD"
    if rule == "input":
        req, response = x["request"], x["response"]
        if req["principal_id"] != x["principal"]:
            return "FORBIDDEN"
        if x["consumed_response"] is not None:
            return "DUPLICATE" if response == x["consumed_response"] else "INPUT_CONFLICT"
        if (req["input_request_id"] != response["input_request_id"]
                or req["expected_revision"] != response["expected_revision"]
                or req["expected_revision"] != x["current_revision"]
                or req["response_type"] != response["response_type"]
                or instant(x["now"]) >= instant(req["expires_at"])):
            return "INPUT_CONFLICT"
        return "ACCEPT"
    if rule == "approval":
        proposal, decision = x["proposal"], x["decision"]
        if any(v != x["authenticated_principal"] for v in (proposal["principal_id"], decision["principal_id"])):
            return "FORBIDDEN"
        if (any(proposal[k] != decision[k] for k in ("proposal_id", "proposal_revision", "operation_id", "payload_hash"))
                or proposal["executor"] != x["executor"] or proposal["audience"] != x["executor"]
                or proposal["payload_hash"] != x["actual_payload_hash"]
                or proposal["resources"] != x["current_resources"]
                or any(r["principal_id"] != proposal["principal_id"] for r in proposal["resources"])
                or instant(x["now"]) >= instant(proposal["expires_at"])):
            return "APPROVAL_CONFLICT"
        if decision["decision"] == "reject":
            return "DENIED"
        return "AUTHORIZED" if x["grant_active"] else "APPROVAL_CONFLICT"
    if rule == "budget":
        parent, child = x["parent"], x["child"]
        if parent["principal_id"] != child["principal_id"]:
            return "FORBIDDEN"
        if (child["parent_reservation_id"] != parent["reservation_id"]
                or child["platform_task_id"] != parent["platform_task_id"]
                or instant(child["deadline"]) > instant(parent["deadline"])
                or any(child[k] + x["already_reserved"][k] > parent[k] for k in ("max_calls", "max_input_tokens", "max_output_tokens"))):
            return "BUDGET_EXHAUSTED"
        return "RESERVE"
    raise ValueError(f"未知规则: {rule}")


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "docs/contracts"
    validators, definitions = {}, {}
    for path in sorted(root.glob("*-v1.schema.json")):
        schema = read(path)
        local_references(schema)
        # 自包含文件可以重复定义，但同名定义不允许在文件之间漂移。
        for name, definition in schema["$defs"].items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("共享定义不一致: " + name)
            definitions[name] = definition
        Draft202012Validator.check_schema(schema)
        validators[path.name] = Draft202012Validator(schema, format_checker=FORMATS)
    if not validators:
        raise ValueError("未找到契约")
    policy = read(root / "ownership-and-transitions.json")
    seen, covered = set(), set()
    for category in ("schema", "semantic"):
        cases = read(root / "fixtures" / (category + "-cases.json"))
        if not cases:
            raise ValueError("测试集为空")
        for case in cases:
            if case["id"] in seen:
                raise ValueError("重复样例 ID: " + case["id"])
            seen.add(case["id"])
            if category == "schema":
                covered.add(case["schema"])
                actual = validators[case["schema"]].is_valid(case["input"])
                expected = case["valid"]
                if type(expected) is not bool:
                    raise ValueError("valid 必须为布尔值")
            else:
                actual = semantic(case["rule"], case["input"], policy)
                expected = case["expected"]
            if actual != expected:
                raise AssertionError(f"{case['id']}: {actual!r} != {expected!r}")
            print(case["id"] + "=" + str(actual).lower() if isinstance(actual, bool) else case["id"] + "=" + actual)
    if covered != set(validators):
        raise ValueError("存在未覆盖的契约")
    print(f"PASS schemas={len(validators)} cases={len(seen)}")


if __name__ == "__main__":
    main()
