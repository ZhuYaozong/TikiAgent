"""正式应用的依赖装配与按需初始化。"""

from __future__ import annotations

from pathlib import Path
import os
from dotenv import load_dotenv

from tikiagent.agents.code import MultiAgentCodeAgent
from tikiagent.agents.research import ResearchAgent
from tikiagent.agents.planning import PlanningSupervisorAgent
from tikiagent.agents.verifier import VerifierAgent
from tikiagent.context.compression.llm import SummaryEngine, LLMBaseCompressor, LLMLocalCompressor
from tikiagent.context.compression.models import ContextBudget
from tikiagent.context.preparation import ContextRuntime
from tikiagent.context.profiles import DEFAULT_CONTEXT_PROFILES
from tikiagent.harness.persistence.finalization import FinalizationLedger
from tikiagent.harness.persistence.budget import RequestBudget
from tikiagent.runtime.policy import AgentPolicy, output_limit, stage_for
from tikiagent.runtime.guard import workspace_revision
from tikiagent.providers.llm.staged import StageModel
from tikiagent.application.models import EventScope
from tikiagent.application.chat import ModelChatService
from tikiagent.application.context_refs import SessionContextReferenceProvider
from tikiagent.application.controller import ApplicationController
from tikiagent.application.events import EventBus
from tikiagent.application.harness_events import HarnessEventForwarder
from tikiagent.application.routing import RuleBasedIntentRouter, StructuredIntentRouter
from tikiagent.application.session import (
    JsonSessionStore,
    JsonlTurnStore,
    SessionService,
)
from tikiagent.application.workflow_adapter import TikiWorkflowAdapter
from tikiagent.context.memory.history import JsonlHistoryStore
from tikiagent.context.models import BaseContext
from tikiagent.harness.coordinator import ExecutionCoordinator
from tikiagent.harness.execution import ExecutionHarness
from tikiagent.harness.permissions.policy import RuleBasedPermissionPolicy, DEFAULT_ALLOWED_TOOLS
from tikiagent.harness.persistence.checkpoint import JsonCheckpointStore
from tikiagent.harness.persistence.trace import JsonlTraceStore
from tikiagent.harness.scope import ExecutionContext
from tikiagent.harness.workspace import Workspace
from tikiagent.orchestration.contracts import Handoff, ResearchResult
from tikiagent.orchestration.workflow import MultiAgentWorkflow
from tikiagent.providers.llm.config import ModelSettings
from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient
from tikiagent.providers.search.config import SearchSettings
from tikiagent.providers.search.tavily import TavilyProvider
from tikiagent.runtime.resumable import ResumableReActAgent
from tikiagent.tools.commands import register_command_tool
from tikiagent.tools.dispatcher import Dispatcher
from tikiagent.tools.models import ToolExecutionError
from tikiagent.tools.files import build_file_registry, build_read_only_file_registry
from tikiagent.tools.web import build_web_registry
from tikiagent.tools.python_environment import register_python_environment_tools
from tikiagent.verification.gate import VerificationGate


CODE_SYSTEM_PROMPT = """你是 TikiAgent CodeAgent。
只执行 Supervisor 当前 Handoff，不负责宣布整个任务完成。
必须通过 Harness 工具观察并修改当前 Session Workspace。
只读 inspection 任务只需取证并回答，不创建文件或运行无关测试。
宿主端保存位置由本地 /paths 命令展示，不能从 Workspace 的搜索失败推断不存在。
Python 命令别名由 Runtime 绑定到项目解释器，不要用任意命令绕过文件工具。
如果 Base Context 包含 ResearchResult，只使用其结构化事实与来源。
不得访问 Workspace 外路径；完成后重新读取文件或运行测试。
最终是否通过由独立 Verification Gate 决定。
"""


class LazyOpenAICompatibleClient:
    """允许 new-session/status 等本地命令在没有 API Key 时运行。"""

    def __init__(self, env_file: str | Path) -> None:
        self.env_file = env_file
        self._client: OpenAICompatibleClient | None = None

    def _get(self) -> OpenAICompatibleClient:
        if self._client is None:
            self._client = OpenAICompatibleClient(
                ModelSettings.from_env(self.env_file)
            )
        return self._client

    def complete(self, messages, tool_schemas):
        return self._get().complete(messages, tool_schemas)

    def complete_structured(self, messages, response_type):
        return self._get().complete_structured(messages, response_type)

    def complete_once(self, messages, tool_schemas):
        return self._get().complete_once(messages, tool_schemas)

    def complete_structured_once(self, messages, response_type):
        return self._get().complete_structured_once(messages, response_type)


class LazyResearchAgent:
    """只有 Supervisor 路由到 ResearchAgent 时才加载 Tavily 配置。"""

    def __init__(
        self,
        model: LazyOpenAICompatibleClient,
        env_file: str | Path,
        context_runtime: ContextRuntime | None = None,
        finalizations=None,
        policy=None,
        request_budget=None,
    ) -> None:
        self.model = model
        self.env_file = env_file
        self.agent: ResearchAgent | None = None
        self.supports_harness = True
        self.context_runtime = context_runtime
        self.finalizations = finalizations
        self.policy = policy or AgentPolicy()
        self.request_budget = request_budget

    def run(
        self,
        handoff: Handoff,
        base_context: BaseContext,
        execution_context: ExecutionContext | None = None,
    ) -> ResearchResult:
        if self.agent is None:
            registry = build_web_registry(
                TavilyProvider(SearchSettings.from_env(self.env_file))
            )
            dispatcher = Dispatcher(registry)
            self.agent = ResearchAgent(
                model=self.model,
                structured_model=self.model,
                dispatcher=dispatcher,
                execution_harness=ExecutionHarness(dispatcher),
                context_runtime=self.context_runtime,
                finalizations=self.finalizations,
                max_steps=self.policy.research_steps,
                max_searches=self.policy.research_searches,
                max_extracts=self.policy.research_extracts,
                request_budget=self.request_budget,
            )
        return self.agent.run(
            handoff,
            base_context,
            execution_context=execution_context,
        )


class ApplicationRuntimeFactory:
    """每个进程重建 Runtime；持久事实由本地 Stores 重新连接。"""

    def __init__(
        self,
        data_dir: str | Path = ".tiki",
        *,
        env_file: str | Path = ".env",
        event_bus: EventBus | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.env_file = env_file
        self.event_bus = event_bus or EventBus()
        self.model = LazyOpenAICompatibleClient(env_file)
        self.checkpoints = JsonCheckpointStore(self.data_dir / "checkpoints")
        load_dotenv(env_file)
        self.policy = AgentPolicy.from_env()
        self.request_budget = RequestBudget(self.data_dir / "budgets", model_limit=self.policy.model_requests,
                                           web_limit=self.policy.task_web_tools)

    def build_controller(self) -> ApplicationController:
        sessions = SessionService(
            sessions=JsonSessionStore(self.data_dir / "sessions"),
            turns=JsonlTurnStore(self.data_dir / "turns"),
        )
        adapter = TikiWorkflowAdapter(
            workflow_factory=self._build_workflow,
            checkpoint_store=self.checkpoints,
            event_bus=self.event_bus,
            request_budget=self.request_budget,
        )
        controller = ApplicationController(
            sessions=sessions,
            router=StructuredIntentRouter(
                StageModel(self.model, "router", account=self.request_budget),
                fallback=RuleBasedIntentRouter(),
            ),
            chat=ModelChatService(StageModel(self.model, "chat", account=self.request_budget)),
            workflow=adapter,
            context_refs=SessionContextReferenceProvider(
                self.data_dir / "histories"
            ),
            event_bus=self.event_bus,
        )
        controller.request_budget = self.request_budget
        return controller

    def _build_workflow(self, session_id: str, workspace_id: str) -> MultiAgentWorkflow:
        # 预算配置不要求密钥；保持本地 status/测试和依赖装配的惰性初始化。
        load_dotenv(self.env_file)
        context_limit = int(os.getenv("TIKI_LLM_CONTEXT_LIMIT", "32000"))
        output_tokens = int(os.getenv("TIKI_LLM_MAX_OUTPUT_TOKENS", "2000"))
        trace = JsonlTraceStore(self.data_dir / "traces" / f"{session_id}.jsonl")

        def observe(event_type, state, correlation_id, data):
            # 编排事件源于实际操作；统一脱敏分发，Trace 不承担恢复职责。
            scope = EventScope(session_id=session_id, workspace_id=workspace_id, task_id=state.get("task_id"))
            message = f"{data['tool_name']}: {event_type}" if data.get("tool_name") else event_type
            event = self.event_bus.emit(event_type, scope=scope, source="workflow_runtime_adapter",
                                       correlation_id=correlation_id, message=message, data=data)
            try:
                trace.append(run_id=state.get("task_id") or session_id, event_type=event_type,
                             tool_call_id=correlation_id, details=event.data)
            except OSError:
                # Trace 落盘失败不能改变实际执行或 Checkpoint 的恢复事实。
                pass

        def context_observer(base, data):
            if base.working_memory.task_id and base.working_memory.session_id:
                self.request_budget.bind(base.working_memory.session_id, base.working_memory.task_id)
            observe("context_prepared", {"task_id": base.working_memory.task_id or None},
                    base.working_memory.task_id or session_id, data)

        def context_runtime(*, code=False):
            engine = SummaryEngine(model("summary"), input_budget=max(256, context_limit - output_limit("summary") - 2000))
            profiles = None
            if code:
                profile = DEFAULT_CONTEXT_PROFILES["code_agent"]
                profiles = {"code_agent": profile.model_copy(update={"system_rules": [CODE_SYSTEM_PROMPT.strip(), *profile.system_rules]})}
            return ContextRuntime(
                budget=ContextBudget(model_context_limit=context_limit, reserved_output_tokens=output_tokens),
                profiles=profiles, base_compressor=LLMBaseCompressor(engine), local_compressor=LLMLocalCompressor(engine),
                observer=context_observer,
                budget_resolver=lambda agent, phase: ContextBudget(model_context_limit=context_limit - 2000,
                    reserved_output_tokens=output_limit(stage_for(agent, phase))),
            )

        def model(stage):
            def diagnostics(data):
                scope = self.request_budget.scope.get()
                observe("model_response", {"task_id": scope[1] if scope else None},
                        scope[1] if scope else session_id, data)
            return StageModel(self.model, stage, account=self.request_budget, observer=diagnostics)

        # 物理目录按 Session 隔离；workspace_id 仍用于 Scope/Approval 身份绑定。
        workspace = Workspace(self.data_dir / "workspaces" / session_id)
        code_registry = build_file_registry(workspace)
        register_command_tool(code_registry, workspace)
        register_python_environment_tools(code_registry, workspace)
        code_dispatcher = Dispatcher(code_registry)
        coordinator = ExecutionCoordinator(
            ExecutionHarness(code_dispatcher, permission_policy=RuleBasedPermissionPolicy(
                allowed_tools=DEFAULT_ALLOWED_TOOLS | {"inspect_python_environment", "probe_python_import"})),
            self.checkpoints,
            trace,
            lifecycle_observer=HarnessEventForwarder(self.event_bus),
        )
        code_agent = MultiAgentCodeAgent(
            ResumableReActAgent(
                model=model("code_agent"),
                dispatcher=code_dispatcher,
                system_prompt=CODE_SYSTEM_PROMPT,
                max_steps=self.policy.code_steps,
                max_tool_calls=self.policy.code_tools,
                repeated_failure_limit=self.policy.repeat_limit,
                progress_revision=lambda: workspace_revision(workspace.root),
                execution_coordinator=coordinator,
                context_runtime=context_runtime(code=True),
            )
        )

        # 验证 Agent 仅拥有受控取证入口，Gate 仍负责身份与验收完整性硬检查。
        verifier_registry = build_read_only_file_registry(workspace)
        register_python_environment_tools(verifier_registry, workspace, tests=True)
        finalizations = FinalizationLedger(self.checkpoints.root / "finalizations")
        verifier = VerifierAgent(model("verifier"), verifier_registry, context_runtime=context_runtime(), observer=observe,
            finalizations=finalizations, max_steps=self.policy.verifier_steps, max_tool_calls=self.policy.verifier_tools,
            deduplicate=True, progress_revision=lambda: workspace_revision(workspace.root))
        gate = VerificationGate(
            research_verifier=verifier,
            code_verifier=verifier,
        )
        def artifact_guard(todo, result):
            """接受交付不豁免机械边界：产物必须真实存在且位于 Workspace。"""
            if todo.owner != "code_agent" or todo.delivery_mode != "artifact":
                return []
            files = result.get("changed_files", [])
            if not files:
                return ["文件交付未提供实际产物路径"]
            blockers = []
            for path in files:
                try:
                    if not workspace.resolve(path).is_file():
                        blockers.append(f"交付文件不存在：{path}")
                except (ToolExecutionError, OSError, ValueError):
                    blockers.append(f"交付文件不在 Workspace 边界内：{path}")
            return blockers

        workflow = MultiAgentWorkflow(
            supervisor=PlanningSupervisorAgent(model("supervisor"), context_runtime=context_runtime(), observer=observe, finalizations=finalizations,
                max_steps=self.policy.supervisor_steps, max_tool_calls=self.policy.supervisor_tools),
            research_agent=LazyResearchAgent(model("research_agent"), self.env_file, context_runtime=context_runtime(), finalizations=finalizations,
                policy=self.policy, request_budget=self.request_budget),
            code_agent=code_agent,
            verification_gate=gate,
            artifact_guard=artifact_guard,
            workspace_id=workspace_id,
            max_delegations=self.policy.delegations,
            history_store=JsonlHistoryStore(
                self.data_dir / "histories" / f"{session_id}.jsonl"
            ),
        )
        workflow.task_code_tools = self.policy.task_code_tools
        return workflow
