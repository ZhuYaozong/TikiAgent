"""通过真实网络与官方 SDK 验证协议；结果不记录认证凭据或业务正文。"""
import asyncio
from copy import deepcopy
import importlib.metadata as md
import json
from pathlib import Path
import platform
import rfc8785
from jsonschema import Draft202012Validator
from uuid import uuid4

from google.protobuf.json_format import MessageToDict
import httpx
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError, TaskNotCancelableError, ExtensionSupportRequiredError

from client import BASE, connect, request, cancel
from fixtures import command, response, rehash, cancellation
from mapping import EXT, command_hash, CONTRACTS

RESULTS = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    RESULTS.append({"case": name, "passed": True})
    print("PASS", name, flush=True)


async def fails(name, awaitable):
    expected = TaskNotFoundError if name in {"cross_user_get", "cross_user_cancel", "cross_user_subscribe", "cross_user_respond"} else TaskNotCancelableError if name in {"completed_not_cancelable", "unsupported_cancel"} else ExtensionSupportRequiredError if name == "missing_required_extension" else InvalidParamsError
    try:
        await awaitable
    except expected:
        check(name, True)
    else:
        check(name, False)


async def consume(stream):
    return [event async for event in stream]


async def poll(client, task_id):
    for _ in range(80):
        task = await client.get_task(p.GetTaskRequest(id=task_id))
        if task.status.state in (p.TASK_STATE_COMPLETED, p.TASK_STATE_CANCELED):
            return task
        await asyncio.sleep(0.05)
    raise TimeoutError("task did not finish")


async def main():
    http, client = await connect()
    bob_http, bob = await connect("bob")
    try:
        health = (await http.get(BASE + "/health")).json()
        check("official_sdk_card_and_binding", client.agent_card.supported_interfaces[0].protocol_version == "1.0")
        c = command()
        sent = await client.send_message(request(c))
        task = sent.task
        check("submit_returns_task_and_distinct_ids", bool(task.id) and task.id != c["target_task_id"] and MessageToDict(task.metadata)[EXT]["domain_task_id"] == c["target_task_id"])
        check("structured_artifact_returned", task.status.state == p.TASK_STATE_COMPLETED and len(task.artifacts) == 1 and task.artifacts[0].parts[0].WhichOneof("content") == "data")
        artifact = MessageToDict(task.artifacts[0].parts[0])["data"]
        validator = Draft202012Validator(json.loads((CONTRACTS / "agent-result-v1.schema.json").read_text(encoding="utf-8")))
        check("artifact_matches_b02_schema", not list(validator.iter_errors(artifact)))
        check("artifact_identity_and_ids_match", artifact["principal_id"] == "alice" and artifact["remote_task_id"] == c["target_task_id"] and artifact["delegation_id"] == c["payload"]["delegation_id"])
        check("get_task", (await client.get_task(p.GetTaskRequest(id=task.id))).id == task.id)
        check("duplicate_returns_original_task", (await client.send_message(request(c))).task.id == task.id)
        altered_id = deepcopy(c)
        altered_id["command_id"] = "changed-command"
        check("command_id_cannot_bypass_dedup", (await client.send_message(request(altered_id))).task.id == task.id)
        changed = deepcopy(c)
        changed["payload"]["instruction"] = "input"
        await fails("same_key_different_hash_conflict", client.send_message(request(rehash(changed))))
        changed = deepcopy(c)
        changed["idempotency_key"] = uuid4().hex
        await fails("same_domain_id_different_key_rejected", client.send_message(request(changed)))
        check("new_logical_request_gets_new_task", (await client.send_message(request(command()))).task.id != task.id)
        concurrent = command(mode="input")
        twins = await asyncio.gather(*(client.send_message(request(concurrent)) for _ in range(2)))
        check("concurrent_duplicates_one_task", twins[0].task.id == twins[1].task.id)

        # 消费前主动丢弃一次 HTTP 响应，再用同一命令重试；不触发第二个逻辑任务。
        lost = command()
        async with http.stream("POST", BASE + "/rpc", json={"jsonrpc": "2.0", "id": "lost", "method": "SendMessage", "params": MessageToDict(request(lost))}) as wire:
            check("discard_response_http_accepted", wire.status_code == 200)
        recovered = (await client.send_message(request(lost))).task
        check("discarded_response_retry_stable", (await client.send_message(request(lost))).task.id == recovered.id)

        for mode, state, kind in (("input", p.TASK_STATE_INPUT_REQUIRED, "text"), ("auth", p.TASK_STATE_AUTH_REQUIRED, "authentication_reference")):
            initial = command(mode=mode)
            waiting = (await client.send_message(request(initial))).task
            check(mode + "_wait_state", waiting.status.state == state)
            metadata = MessageToDict(waiting.metadata)
            answer = response(initial, metadata["input_request_id"], kind)
            bad = deepcopy(answer)
            bad["payload"]["input_request_id"] = "wrong"
            await fails(mode + "_wrong_input_id", client.send_message(request(rehash(bad), waiting.id)))
            done = (await client.send_message(request(answer, waiting.id))).task
            check(mode + "_continues_same_task", done.id == waiting.id and done.status.state == p.TASK_STATE_COMPLETED)
            check(mode + "_duplicate_response", (await client.send_message(request(answer, waiting.id))).task.id == waiting.id)
            new_answer = response(initial, metadata["input_request_id"], kind)
            await fails(mode + "_terminal_cannot_reopen", client.send_message(request(new_answer, waiting.id)))

        waiting_cmd = command(mode="input")
        waiting = (await client.send_message(request(waiting_cmd))).task
        await fails("cross_user_get", bob.get_task(p.GetTaskRequest(id=waiting.id)))
        await fails("cross_user_cancel", bob.cancel_task(p.CancelTaskRequest(id=waiting.id)))
        await fails("cross_user_subscribe", consume(bob.subscribe(p.SubscribeToTaskRequest(id=waiting.id))))
        stolen = response(command("bob"), MessageToDict(waiting.metadata)["input_request_id"])
        await fails("cross_user_respond", bob.send_message(request(stolen, waiting.id)))
        cancel_command = cancellation(waiting_cmd)
        await fails("cancel_missing_command_rejected", client.cancel_task(p.CancelTaskRequest(id=waiting.id)))
        stale = deepcopy(cancel_command)
        stale["payload"]["expected_revision"] = 99
        await fails("cancel_stale_revision_rejected", cancel(client, waiting.id, rehash(stale)))
        canceled = await cancel(client, waiting.id, cancel_command)
        check("cancel_waiting_task", canceled.status.state == p.TASK_STATE_CANCELED)
        check("repeat_cancel_stable", (await cancel(client, waiting.id, cancel_command)).status.state == p.TASK_STATE_CANCELED)
        conflict = deepcopy(cancel_command)
        conflict["payload"]["reason"] = "different reason"
        await fails("cancel_same_key_conflict", cancel(client, waiting.id, rehash(conflict)))
        await fails("completed_not_cancelable", cancel(client, task.id, cancellation(c)))
        un_command = command(mode="uncancelable")
        un = (await client.send_message(request(un_command))).task
        await fails("unsupported_cancel", cancel(client, un.id, cancellation(un_command)))

        events = await consume(client.send_message_streaming(request(command(mode="slow"))))
        check("stream_has_task_artifact_terminal", any(e.HasField("task") for e in events) and any(e.HasField("artifact_update") for e in events) and events[-1].status_update.status.state == p.TASK_STATE_COMPLETED)
        stream = client.send_message_streaming(request(command(mode="slow")))
        first = await anext(stream)
        await stream.aclose()
        final = await poll(client, first.task.id)
        check("stream_disconnect_does_not_cancel", final.status.state == p.TASK_STATE_COMPLETED)
        await fails("terminal_subscription_rejected", consume(client.subscribe(p.SubscribeToTaskRequest(id=final.id))))
        slow = (await client.send_message(request(command(mode="slow")))).task
        resumed = await consume(client.subscribe(p.SubscribeToTaskRequest(id=slow.id)))
        check("active_subscription_completes", resumed[-1].status_update.status.state == p.TASK_STATE_COMPLETED)
        race_command = command(mode="slow")
        race = (await client.send_message(request(race_command))).task
        await cancel(client, race.id, cancellation(race_command))
        await asyncio.sleep(0.8)
        check("late_completion_cannot_override_cancel", (await client.get_task(p.GetTaskRequest(id=race.id))).status.state == p.TASK_STATE_CANCELED)

        forged = command("bob")
        await fails("message_identity_cannot_override_token", client.send_message(request(forged)))
        bad = command()
        bad["payload"]["resources"][0]["principal_id"] = "bob"
        await fails("cross_owner_resource_rejected", client.send_message(request(rehash(bad))))
        bad = command()
        bad["payload"]["skill_id"] = "job.resume.tailor.v1"
        await fails("unapproved_skill_rejected", client.send_message(request(rehash(bad))))
        bad = command()
        bad["payload_hash"] = "sha256:" + "0" * 64
        await fails("false_hash_rejected", client.send_message(request(bad)))
        bad_req = request(command())
        bad_req.metadata[EXT]["version"] = "99"
        await fails("unknown_binding_version", client.send_message(bad_req))
        await fails("client_chosen_new_a2a_task_id_rejected", client.send_message(request(command(), "invented")))
        bad_req = request(command())
        bad_req.message.context_id = task.context_id
        await fails("untrusted_context_rejected", client.send_message(bad_req))
        headers = dict(http.headers)
        del http.headers["A2A-Extensions"]
        await fails("missing_required_extension", client.get_task(p.GetTaskRequest(id=task.id)))
        http.headers.update(headers)
        async with httpx.AsyncClient(trust_env=False) as anonymous:
            for name, auth in (("missing_token", ""), ("invalid_token", "Bearer wrong")):
                wire = await anonymous.post(BASE + "/rpc", headers={"Authorization": auth}, json={"jsonrpc": "2.0", "id": "1", "method": "GetTask", "params": {"id": task.id}})
                check(name, wire.status_code == 401)
        # JCS 证据覆盖键序与数字表示；不把普通 sort_keys 当作 JCS。
        reordered = {k: c[k] for k in reversed(c)}
        check("jcs_key_order_stable", command_hash(reordered) == command_hash(c))
        check("jcs_number_canonicalization", rfc8785.dumps({"n": 1.0, "z": -0.0}) == b'{"n":1,"z":0}')
        for identity_field, value in (("audience", "tiki"), ("actor", "platform")):
            bad = command()
            bad["identity"][identity_field] = value
            await fails("wrong_" + identity_field, client.send_message(request(rehash(bad))))
        bad = command()
        bad["payload"]["skill_id"] = "unknown.skill"
        await fails("unknown_skill_rejected", client.send_message(request(rehash(bad))))
        check("bob_legitimate_task_is_distinct", (await bob.send_message(request(command("bob")))).task.id != task.id)
        result = {"client_python": platform.python_version(), "server": health, "sdk": md.version("a2a-sdk"), "cases": RESULTS}
        path = Path(__file__).parent / ".runtime/results.json"
        path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"{len(RESULTS)} cases passed", flush=True)
    finally:
        await http.aclose()
        await bob_http.aclose()


if __name__ == "__main__":
    asyncio.run(main())
