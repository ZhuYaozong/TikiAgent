"""正式应用的依赖装配与按需初始化。"""

from __future__ import annotations

from pathlib import Path
import sys

from tikiagent.agents.code import MultiAgentCodeAgent
from tikiagent.agents.research import ResearchAgent
from tikiagent.agents.supervisor import SupervisorAgent
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
from tikiagent.harness.permissions.policy import FixedCommandPermissionPolicy
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
from tikiagent.tools.files import build_file_registry, build_read_only_file_registry
from tikiagent.tools.web import build_web_registry
from tikiagent.verification.artifacts import ArtifactAwareCodeVerifier
from tikiagent.verification.environment import CommandCheck
from tikiagent.verification.gate import VerificationGate
from tikiagent.verification.research import ResearchResultVerifier


CODE_SYSTEM_PROMPT = """你是 TikiAgent CodeAgent。
只执行 Supervisor 当前 Handoff，不负责宣布整个任务完成。
必须通过 Harness 工具观察并修改当前 Session Workspace。
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


class LazyResearchAgent:
    """只有 Supervisor 路由到 ResearchAgent 时才加载 Tavily 配置。"""

    def __init__(
        self,
        model: LazyOpenAICompatibleClient,
        env_file: str | Path,
    ) -> None:
        self.model = model
        self.env_file = env_file
        self.agent: ResearchAgent | None = None
        self.supports_harness = True

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

    def build_controller(self) -> ApplicationController:
        sessions = SessionService(
            sessions=JsonSessionStore(self.data_dir / "sessions"),
            turns=JsonlTurnStore(self.data_dir / "turns"),
        )
        adapter = TikiWorkflowAdapter(
            workflow_factory=self._build_workflow,
            checkpoint_store=self.checkpoints,
            event_bus=self.event_bus,
        )
        return ApplicationController(
            sessions=sessions,
            router=StructuredIntentRouter(
                self.model,
                fallback=RuleBasedIntentRouter(),
            ),
            chat=ModelChatService(self.model),
            workflow=adapter,
            context_refs=SessionContextReferenceProvider(
                self.data_dir / "histories"
            ),
            event_bus=self.event_bus,
        )

    def _build_workflow(self, session_id: str, workspace_id: str) -> MultiAgentWorkflow:
        # 物理目录按 Session 隔离；workspace_id 仍用于 Scope/Approval 身份绑定。
        workspace = Workspace(self.data_dir / "workspaces" / session_id)
        code_registry = build_file_registry(workspace)
        register_command_tool(code_registry, workspace)
        code_dispatcher = Dispatcher(code_registry)
        coordinator = ExecutionCoordinator(
            ExecutionHarness(code_dispatcher),
            self.checkpoints,
            JsonlTraceStore(self.data_dir / "traces" / f"{session_id}.jsonl"),
            lifecycle_observer=HarnessEventForwarder(self.event_bus),
        )
        code_agent = MultiAgentCodeAgent(
            ResumableReActAgent(
                model=self.model,
                dispatcher=code_dispatcher,
                system_prompt=CODE_SYSTEM_PROMPT,
                max_steps=12,
                execution_coordinator=coordinator,
            )
        )

        # Verifier 使用 Artifact 类型选择确定性检查，不执行模型生成的验证命令。
        verifier_registry = build_read_only_file_registry(workspace)
        register_command_tool(verifier_registry, workspace)
        verifier_dispatcher = Dispatcher(verifier_registry)
        command = (
            sys.executable,
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            ".",
            "-p",
            "test_*.py",
            "-v",
        )
        gate = VerificationGate(
            research_verifier=ResearchResultVerifier(min_sources=1),
            code_verifier=ArtifactAwareCodeVerifier(
                dispatcher=verifier_dispatcher,
                execution_harness=ExecutionHarness(
                    verifier_dispatcher,
                    permission_policy=FixedCommandPermissionPolicy(
                        allowed_commands={command}
                    ),
                ),
                python_test_check=CommandCheck(name="python-unittest", command=command),
            ),
        )
        return MultiAgentWorkflow(
            supervisor=SupervisorAgent(self.model),
            research_agent=LazyResearchAgent(self.model, self.env_file),
            code_agent=code_agent,
            verification_gate=gate,
            workspace_id=workspace_id,
            max_delegations=5,
            history_store=JsonlHistoryStore(
                self.data_dir / "histories" / f"{session_id}.jsonl"
            ),
        )
