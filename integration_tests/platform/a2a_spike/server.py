"""官方 A2A 路由 + 应用 handler；内存任务仅用于协议验证。"""
import asyncio
import base64
import json
from contextlib import asynccontextmanager
from copy import deepcopy
import importlib.metadata as md
from uuid import uuid4

from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import a2a_pb2 as p
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError, TaskNotCancelableError, UnsupportedOperationError, ExtensionSupportRequiredError

from fixtures import TOKENS, template
from mapping import EXT, validate

URL = "http://127.0.0.1:38446"
TERMINAL = {p.TASK_STATE_COMPLETED, p.TASK_STATE_CANCELED, p.TASK_STATE_FAILED, p.TASK_STATE_REJECTED}
CARD = ParseDict({"name": "B05 JobPilot fixture", "description": "Disposable protocol compatibility probe", "version": "1.0.0", "supportedInterfaces": [{"url": URL + "/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}], "capabilities": {"streaming": True, "extensions": [{"uri": EXT, "required": True, "description": "B02 command binding v1"}]}, "defaultInputModes": ["application/json"], "defaultOutputModes": ["application/json"], "skills": [{"id": "job.resume.read.v1", "name": "Read selected resume", "description": "Fixture only", "tags": ["read"]}], "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}}, "securityRequirements": [{"schemes": {"bearer": {"list": []}}}]}, p.AgentCard())


class Auth:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/rpc":
            headers = dict(scope["headers"])
            token = headers.get(b"authorization", b"").decode()
            if token.removeprefix("Bearer ") not in TOKENS or not token.startswith("Bearer "):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class Handler(RequestHandler):
    def __init__(self):
        self.tasks, self.receipts, self.domains = {}, {}, {}
        self.lock = asyncio.Lock()
        self.jobs = set()

    def principal(self, context):
        if EXT not in context.requested_extensions:
            raise ExtensionSupportRequiredError()
        return TOKENS[context.state["headers"]["authorization"].removeprefix("Bearer ")]

    def owned(self, task_id, principal):
        record = self.tasks.get(task_id)
        if record is None or record["principal"] != principal:
            raise TaskNotFoundError()
        return record

    def complete(self, record):
        if record["task"].status.state in TERMINAL:
            return
        record["task"].status.state = p.TASK_STATE_COMPLETED
        result = template("agent-result-v1.schema.json")
        # 完整 B02 结果结构来自冻结样例，仅替换身份与任务引用。
        for field in ("principal_id", "platform_task_id"):
            if field in result:
                result[field] = record["command"]["identity"]["principal_id"] if field == "principal_id" else record["command"][field]
        result.update(result_id="result-" + record["task"].id, run_id=record["command"]["payload"]["run_id"], delegation_id=record["command"]["payload"]["delegation_id"], remote_task_id=record["command"]["target_task_id"])
        for reference in [result["artifacts"][0]["resource"], *result["evidence_refs"], *result["source_versions"]]:
            reference["principal_id"] = record["principal"]
        record["task"].artifacts.append(ParseDict({"artifactId": "result-" + record["task"].id, "parts": [{"data": result}], "extensions": [EXT]}, p.Artifact()))
        record["task"].metadata.update({"revision": 2})
        record["done"].set()

    async def finish_later(self, record):
        # 后台执行不隶属于 SSE 订阅；这不是持久任务队列。
        await asyncio.sleep(0.6)
        async with self.lock:
            self.complete(record)

    async def on_message_send(self, params, context):
        principal = self.principal(context)
        metadata = MessageToDict(params.metadata)
        if set(metadata) != {EXT} or not isinstance(metadata[EXT], dict) or set(metadata[EXT]) != {"version", "command"} or metadata[EXT]["version"] != "1.0":
            raise InvalidParamsError("UNKNOWN_BINDING")
        command = metadata[EXT]["command"]
        key = validate(command, principal)
        message = params.message
        if message.role != p.ROLE_USER or message.context_id or message.reference_task_ids:
            raise InvalidParamsError("INVALID_CONTEXT_OR_ROLE")
        if len(message.parts) != 1 or message.parts[0].text != "B02 command envelope":
            raise InvalidParamsError("AMBIGUOUS_PAYLOAD")
        async with self.lock:
            previous = self.receipts.get(key)
            if previous:
                old_hash, task_id = previous
                if command["payload_hash"] != old_hash:
                    raise InvalidParamsError("IDEMPOTENCY_CONFLICT")
                if command["kind"] == "respond" and message.task_id != task_id:
                    raise InvalidParamsError("TASK_BINDING_MISMATCH")
                if command["kind"] == "delegate" and message.task_id:
                    raise InvalidParamsError("NEW_TASK_ID_MUST_BE_EMPTY")
                return deepcopy(self.owned(task_id, principal)["task"])
            if command["kind"] == "delegate":
                if message.task_id:
                    raise InvalidParamsError("NEW_TASK_ID_MUST_BE_EMPTY")
                domain_key = (principal, command["target_task_id"])
                if domain_key in self.domains:
                    raise InvalidParamsError("DOMAIN_TASK_ALREADY_BOUND")
                mode = command["payload"]["instruction"]
                if mode not in {"complete", "input", "auth", "slow", "uncancelable"}:
                    raise InvalidParamsError("UNKNOWN_FIXTURE")
                state = p.TASK_STATE_INPUT_REQUIRED if mode == "input" else p.TASK_STATE_AUTH_REQUIRED if mode == "auth" else p.TASK_STATE_WORKING
                task = p.Task(id=uuid4().hex, context_id=uuid4().hex, status=p.TaskStatus(state=state))
                task.metadata.update({EXT: {"domain_task_id": command["target_task_id"], "delegation_id": command["payload"]["delegation_id"]}, "revision": 1})
                record = {"task": task, "principal": principal, "command": command, "mode": mode, "done": asyncio.Event()}
                if mode in ("input", "auth"):
                    record["input_id"] = "input-" + uuid4().hex
                    task.metadata.update({"input_request_id": record["input_id"], "wait_kind": "clarification" if mode == "input" else "authentication"})
                self.tasks[task.id] = record
                self.domains[domain_key] = task.id
                if mode == "complete":
                    self.complete(record)
                elif mode == "slow":
                    job = asyncio.create_task(self.finish_later(record))
                    self.jobs.add(job)
                    job.add_done_callback(self.jobs.discard)
            elif command["kind"] == "respond":
                record = self.owned(message.task_id, principal)
                task = record["task"]
                if command["target_task_id"] != record["command"]["target_task_id"] or command["platform_task_id"] != record["command"]["platform_task_id"]:
                    raise InvalidParamsError("TASK_BINDING_MISMATCH")
                if task.status.state in TERMINAL:
                    raise InvalidParamsError("TERMINAL_TASK")
                payload = command["payload"]
                expected_type = "authentication_reference" if record["mode"] == "auth" else "text"
                if payload["input_request_id"] != record.get("input_id") or payload["expected_revision"] != 1 or payload["response_type"] != expected_type:
                    raise InvalidParamsError("INPUT_REQUEST_MISMATCH")
                self.complete(record)
                for field in ("wait_kind", "input_request_id"):
                    if field in task.metadata:
                        del task.metadata[field]
            else:
                raise InvalidParamsError("UNSUPPORTED_COMMAND")
            self.receipts[key] = (command["payload_hash"], task.id)
            return deepcopy(task)

    async def on_get_task(self, params, context):
        return deepcopy(self.owned(params.id, self.principal(context))["task"])

    async def on_cancel_task(self, params, context):
        async with self.lock:
            principal = self.principal(context)
            record = self.owned(params.id, principal)
            task = record["task"]
            encoded = context.state["headers"].get("x-tiki-command", "")
            if not encoded or len(encoded) > 8192:
                raise InvalidParamsError("MISSING_CANCEL_COMMAND")
            try:
                command = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True))
            except (ValueError, UnicodeError):
                raise InvalidParamsError("INVALID_CANCEL_COMMAND")
            key = validate(command, principal)
            if command["kind"] != "cancel" or command["target_task_id"] != record["command"]["target_task_id"] or command["platform_task_id"] != record["command"]["platform_task_id"]:
                raise InvalidParamsError("TASK_BINDING_MISMATCH")
            if key in self.receipts:
                if self.receipts[key] != (command["payload_hash"], task.id):
                    raise InvalidParamsError("IDEMPOTENCY_CONFLICT")
                return deepcopy(task)
            if task.status.state in TERMINAL or record["mode"] == "uncancelable":
                raise TaskNotCancelableError()
            if command["payload"]["expected_revision"] != int(task.metadata["revision"]):
                raise InvalidParamsError("STALE_REVISION")
            task.status.state = p.TASK_STATE_CANCELED
            task.metadata.update({"revision": 2})
            for field in ("wait_kind", "input_request_id"):
                if field in task.metadata:
                    del task.metadata[field]
            record["done"].set()
            self.receipts[key] = (command["payload_hash"], task.id)
            return deepcopy(task)

    async def events(self, record):
        yield deepcopy(record["task"])
        if record["task"].status.state in TERMINAL or record["mode"] in ("input", "auth"):
            return
        await asyncio.wait_for(record["done"].wait(), timeout=10)
        for artifact in record["task"].artifacts:
            yield p.TaskArtifactUpdateEvent(task_id=record["task"].id, context_id=record["task"].context_id, artifact=artifact)
        yield p.TaskStatusUpdateEvent(task_id=record["task"].id, context_id=record["task"].context_id, status=record["task"].status)

    async def on_message_send_stream(self, params, context):
        task = await self.on_message_send(params, context)
        async for event in self.events(self.tasks[task.id]):
            yield event

    async def on_subscribe_to_task(self, params, context):
        record = self.owned(params.id, self.principal(context))
        if record["task"].status.state in TERMINAL:
            raise InvalidParamsError("TERMINAL_SUBSCRIPTION")
        async for event in self.events(record):
            yield event

    async def unsupported(self, params, context):
        self.principal(context)
        raise UnsupportedOperationError()

    on_list_tasks = unsupported
    on_get_extended_agent_card = unsupported
    on_create_task_push_notification_config = unsupported
    on_get_task_push_notification_config = unsupported
    on_delete_task_push_notification_config = unsupported
    on_list_task_push_notification_configs = unsupported


handler = Handler()


async def health(request):
    import platform
    return JSONResponse({"probe": "b05", "python": platform.python_version(), "sdk": md.version("a2a-sdk")})


@asynccontextmanager
async def lifespan(app):
    yield
    for job in list(handler.jobs):
        job.cancel()
    await asyncio.gather(*handler.jobs, return_exceptions=True)


app = Auth(Starlette(routes=[Route("/health", health), *create_agent_card_routes(CARD), *create_jsonrpc_routes(handler, "/rpc")], lifespan=lifespan))
