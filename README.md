# TikiAgent

TikiAgent 是一个渐进式构建的 **Multi-Agent Task Execution System**。它使用 Supervisor 根据任务动态调度 ResearchAgent 和 CodeAgent，通过统一 Verification Gate 验证每次 Specialist 交付，再由 Supervisor 决定继续委派或结束。Context Engine 根据当前 Agent、任务阶段和显式引用重新构建 Base Context，避免 Specialist 直接继承全部历史。

当前版本为 **v0.8.0 Textual TUI / Application / Session / Event Stream / CLI**。

## v0.5 Context-aware Multi-Agent 架构

```text
                         Supervisor
                              │
                       Plan / Delegate
                              │
             ┌────────────────┴────────────────┐
             ▼                                 ▼
      ResearchAgent                       CodeAgent
             │                                 │
             └────────────────┬────────────────┘
                              ▼
                     Verification Gate
                              │
                 ┌────────────┴────────────┐
                 ▼                         ▼
       Research Rule Verifier     Code Environment Verifier
                 │                         │
                 └────────────┬────────────┘
                              ▼
                         Supervisor
                         ↙        ↘
                    Delegate      Finish
```

正式主工作流使用 **Graph Routing**：

```text
SupervisorDecision
    ↓
LangGraph Conditional Edge
    ↓
Specialist Node
```

Research-only、Coding-only 和 Hybrid 的 Specialist Result 都必须经过 Verification Gate。Gate 是通用验证入口，不要求每次都调用 LLM：Research 使用来源规则验证，Code 使用只读环境检查。

Context Plane 独立于 Control Plane：

```text
State / Task Board / History / Notepad
                  │
                  ▼
              Retriever
                  ▼
            ContextBuilder
                  ▼
             BaseContext

Profile ─────→ PromptBundle
Agent + Phase → ToolView
Agent Runtime → LocalMemory
                  │
                  ▼
         CandidateModelCall
                  ▼
            ContextMonitor
                  ▼
          CompressionPolicy
          ↙                 ↘
 BaseCompressor       LocalCompressor
          ↘                 ↙
               重新组装
                  ▼
          PreparedModelCall
                  ▼
                 LLM
```

`BaseContext` 只保存“当前拿什么工作”：任务、阶段、当前指令、验收标准、Todo、相关 History 和相关 Notepad。角色与行为规则由 `PromptAssembler` 生成 `PromptBundle`，不会同时在两处重复。

`CandidateModelCall` 是压缩前的完整候选调用；`PreparedModelCall` 是预算检查和必要压缩后可以真正发送的调用。完整输入估算包含：

```text
prompt + base + history + notepad + local
+ tool schemas + response schema
```

模型窗口先减去 `reserved_output_tokens`，剩余空间才是输入预算。v1 使用可替换的字符估算器，后续可以替换为具体模型 Tokenizer。

ResearchAgent 和 CodeAgent 在各自单次 `run()` 内维护 `LocalMemory`：

```text
Base Context
→ assistant ToolCall
→ Tool Observation
→ assistant 下一步
→ Result
```

Local Compressor 在**每一轮 ReAct 模型调用前**检查不断增长的局部交互。Assistant ToolCalls 和对应 `tool_call_id` 的全部 ToolResults 构成不可拆分的 `ReActInteraction`。同一次运行从 coding 切到 debugging 时保留 LocalMemory；Verifier FAIL 后经 Supervisor 重新委派时创建新的 BaseContext 和 LocalMemory。

Base Compressor 和 Local Compressor 使用独立 Protocol。当前默认是确定性 Rule-Based 实现：Base Summary 替换非保护 History 原文；Local Summary 替换旧 Interaction 并保留最近完整交互。LLM Compressor 尚未实现，但不需要改变 Runtime 接口即可扩展。

## v0.6a1 Harness Security Substrate

正式 Harness 在原有 Registry、Dispatcher、Workspace 和 Command Runtime 之上增加执行前安全管线：

```text
Raw ToolCall
    ↓
Basic ToolCall Validation
    ↓
Tool Exposure Guard
    ↓
Dispatcher.prepare()
├── Registry Lookup
├── Arguments Validation
└── Canonical Arguments
    ↓
PermissionPolicy
   /      |       \
ALLOW    ASK      DENY
  │       │         └── stop
  │   ApprovalRequest
  │       ↓
  │   ApprovalDecision
  │       ↓
  └───────┤
          ↓
   before_execute()
          ↓
 Dispatcher.execute()
          ↓
 Workspace / Runtime Enforcement
          ↓
      ToolResult
```

职责保持分离：

```text
ToolSelector
= 向模型展示哪些 Tool Schema

ToolExposureGuard
= 执行侧再次强制本轮能力范围

PermissionPolicy
= 当前规范化 ToolCall 是 ALLOW / ASK / DENY

ApprovalGate
= 外部是否批准这个具体 ToolCall

Dispatcher
= 参数校验、规范化并调用 Python handler

Workspace / Runtime
= 即使已经 ALLOW，也继续强制路径、cwd、timeout 和 output limit
```

`ExecutionScope.workspace_id` 是 Approval 的归属身份；`Workspace Boundary` 是文件路径是否真正逃出根目录的机械检查。两者不是同一个概念。

Permission v1 只分析经过 Pydantic 校验的结构化 argv，不解析任意 Shell 字符串：

```text
python -m pytest / unittest       → ALLOW
pip install / uv add / git commit → ASK
未分类命令                         → DENY
```

Verifier 的固定环境检查使用 `FixedCommandPermissionPolicy`，只有与应用预配置 tuple 完全一致的 argv 才 ALLOW；这不会扩大模型可自由生成的命令范围。

`HarnessOutcome.status` 是唯一状态来源：

```text
completed
→ tool_result != null

awaiting_approval
→ tool_result == null
→ approval_request != null

denied
→ 失败 ToolResult
→ handler 没有执行
```

Approval 同时绑定 `task_id`、`session_id`、`workspace_id`、工具名称和 canonical arguments。Scope mismatch、参数篡改、拒绝后重用和批准重放都会被拒绝。

ASK 产生的是 Harness Runtime 中的待执行状态，不是 LocalMemory Observation：

```text
完整 Assistant ToolCall + ToolResult
→ ReActInteraction
→ LocalMemory

尚未批准的 ToolCall
→ ApprovalRequest / Pending Execution
→ 不进入 LocalMemory
```

`Dispatcher.dispatch()` 为 v0.1～v0.5 Baseline 暂时保留，但它只是 legacy/internal compatibility API，会绕过 Exposure、Permission 和 Approval。v0.6a2 正式 Multi-Agent 示例使用 `ResumableReActAgent`，ResearchAgent 和 Verifier 也显式注入 `ExecutionHarness`；新的正式执行路径不应再调用该兼容入口。

当前真实边界是：

```text
v0.6a1
= Harness security substrate 已完成
≠ 所有 Agent 已强制经过 Permission

v0.6a2
= Checkpoint / Resume / Trace 已接入
+ Resumable Agent Runtime 成为正式执行路径
```

离线演示不会安装真实依赖：

```powershell
uv run --locked python examples/harness_security.py
```

## v0.6a2 Persist / Observe

Checkpoint 是恢复事实的唯一权威来源，Trace 只用于审计和可观测性：

```text
Checkpoint = resume source of truth
Trace      = audit / observability only
History    = 可供未来 Agent 重新检索的任务信息
```

ASK 的持久化顺序被冻结为：

```text
ASK
→ save checkpoint(awaiting_approval)
→ 进程可以安全退出

Approval PASS
→ validate scope + fingerprint
→ save checkpoint(executing)
→ consume approval
→ Trace(tool_execution_started)
→ handler()
→ save checkpoint(completed + real ToolResult)
→ Trace(tool_execution_finished)
```

审批“已批准”和执行“已完成”是两个不同事实。`before_execute()` 保存 `executing` 失败时，handler 不会启动，Approval 也不会被提前消费。`HarnessOutcome.status=completed` 只表示 Harness 生命周期结束；`ToolResult.ok` 和命令 `exit_code` 仍分别表示协议结果与业务结果。

一个原子 Checkpoint 同时保存：

```text
ExecutionCheckpoint
├── ExecutionIdentity
│   ├── run_id
│   ├── execution_id
│   ├── tool_call_id
│   └── attempt
├── WorkflowResumeSnapshot
│   ├── canonical TikiState
│   └── HistoryResumeReference(path + cursor)
└── ReActRunSnapshot
    ├── BaseContext
    ├── LocalMemory
    ├── ordered pending ToolCalls
    ├── completed ToolResults
    └── next_tool_index
```

多 ToolCall 严格按模型返回顺序执行。遇到 ASK 时，只保存已经完成的前缀和下一个位置；在全部 ToolCall 都取得真实 ToolResult 之前，不会形成 `ReActInteraction`，因此 LocalMemory 中不存在孤立 ToolCall。

Resume 不在 Graph 外直接调用 Specialist 后手工跳节点：

```text
load checkpoint
→ restore JSONL History
→ restore TikiState
→ Graph START
→ Resume Entry Router
→ resume_entry Node
→ CodeAgent / Verification / Supervisor
```

`revision` 使用 compare-and-swap 语义防止同一 Checkpoint 被重复 Resume。新进程发现 `executing` 时进入 `recovery_required`，禁止自动重放未知副作用：

```text
confirmed_not_executed
→ 新 execution_id / attempt
→ 重新走 Gate；ASK 重新审批

confirmed_executed
→ awaiting_reconcile
→ 禁止自动继续
→ 人工提供真实 ToolResult + evidence
→ completed(manually_reconciled)
```

`confirmed_executed` 不会伪造成功 ToolResult。没有 `ReconcileResult` 时，Graph 保持暂停。

Checkpoint 使用临时文件、`fsync` 和 `os.replace` 原子替换，并用 SHA-256 发现意外损坏；SHA-256 不是防攻击签名。Trace 使用带 `sequence` 的 JSONL，限制长文本并按字段名脱敏 Secret。正式系统仍不应把 API Key、Authorization 或密码明文写入 Checkpoint，应该保存 Secret reference。

完全离线的持久化演示：

```powershell
uv run --locked python examples/harness_resume.py
```

## v0.7 Application Plane

Application Plane 在 Multi-Agent Workflow 外提供稳定入口，但不复制 Control、Context 或 Harness 的职责：

```text
User / CLI
    ↓
ApplicationController
    ├── Session / Turn Store
    ├── Intent Router ── CHAT ──→ ChatService（无工具）
    └── WORKFLOW ───────────────→ TikiWorkflowAdapter
                                      ↓
                                 MultiAgentWorkflow
                                      ↓
                              Execution Harness Adapter
                                      ↓
                                   EventBus
```

四个应用身份的生命周期不同：

```text
Session = 一段多轮对话与共享 Workspace 的长期容器
Turn    = 一次用户输入及应用响应
Task    = 一个进入 WORKFLOW 的用户目标
Run     = Task 的一次实际 Graph/Agent 执行尝试
```

`SessionRecord` 只保存身份、计数和 `active_checkpoint_id`，不内嵌完整 History、Messages 或 Workflow status。`status/resume/recover/reconcile` 始终加载权威 Checkpoint；Trace 和 Session 缓存都不能参与恢复决策。

暂停顺序固定为：

```text
Workflow / Harness 保存 Checkpoint 成功
        ↓
Session 绑定 active_checkpoint_id
```

同一 Session 的下一 Task 可以显式引用上一 Task 已 Finalize 的 Result：

```text
previous final result_id
        ↓
TikiState.session_context_refs
        ↓
Retriever exact lookup
        ↓
新的 Agent Base Context
```

显式引用允许跨 Task，但禁止跨 Session，仍受 Agent Context Profile 的 record type 限制。Keyword/recent 检索保持当前 Task 作用域。ResearchAgent/CodeAgent 的内部 ReAct messages 不会跨 Agent 或跨 Task 继承。

Intent Router 只判断 `CHAT / WORKFLOW`，不规划 Specialist 或 Tool。CHAT 路径不暴露 Tool Schema；WORKFLOW 的规划、路由与重试仍由 Supervisor 负责。

EventBus 是唯一应用事件工厂，负责 stream 内递增 `sequence`、Secret 脱敏、长文本截断和 Sink 隔离：

```text
ApplicationController → session_started / turn_received / intent_routed / final_answer
WorkflowAdapter       → supervisor / agent / handoff / result / verification
HarnessAdapter        → tool request / approval / execution / result / recovery
```

Controller 不根据最终结果反推 Tool 或 Approval 事件；Harness 生命周期观察接口只转发真实发生的执行事实。Event Stream 用于 UI/CLI 展示，不是 Trace，也不是 Resume source of truth。

## v0.8 Textual TUI

正式 TUI 复用 v0.7 Application Plane，不在 Widget 中重新实现 Workflow：

```text
ApplicationController / Workflow / Harness
                   ↓
              EventBus
                   ↓
            TextualEventSink
                   ↓ post_message()
────────────────────────────────────
          Textual UI 主线程
                   ↓
          TuiEventAdapter
                   ↓
           TuiViewState
                   ↓
                Widgets
```

`TuiViewState` 是可丢弃的显示投影，不是 Session、Checkpoint、Approval Ledger 或恢复事实。Widget 只读取投影字段；同步 Controller、Session Store 和只读 Workspace 扫描均在线程 Worker 中执行。Worker 禁止直接更新 Widget，统一发送 Textual Message 回主线程。

界面包括实时事件时间线、历史对话、Session/Workflow/Runtime 状态、最终回答和只读 Workspace Tree。支持：

```text
/new [workspace]  新建 Session
/session <id>    连接已有 Session
/status          读取权威 Checkpoint 状态
/approval        重新打开审批窗口
/recovery        打开人工恢复窗口
/workspace       刷新只读 Workspace Tree
/help            显示帮助
/quit            关闭 TUI，不取消 Workflow
```

Approval Modal 只把一次用户决定提交给 `ApplicationController.resume()`；Scope、request ID、fingerprint 和 revision 仍由权威 Checkpoint/Harness 校验。`confirmed_executed` 会进入 Reconcile Modal，必须加载真实 `ReconcileSubmission` JSON，不能由 UI 伪造 ToolResult。

Workspace Tree 只返回受 Session 目录约束的文件名、相对路径和类型，不提供打开、编辑或删除 API，也不会跟随符号链接逃出 Workspace。`Ctrl+Q` 只关闭 UI；执行期间退出会明确提示未取消的 Workflow 可能需要恢复。

## Result 与 Verification 身份链

每次委派、交付和验证都通过 ID 明确关联：

```text
Handoff.handoff_id
        ↓
Result.handoff_id + Result.result_id
        ↓
VerificationReport.handoff_id + VerificationReport.result_id
```

Supervisor 的 FINISH Guard 只认可每个必要 Specialist 的**最新 Result**对应的 PASS：

```text
report.passed is True
and report.result_id == latest_result.result_id
and report.handoff_id == latest_result.handoff_id
and report.subject_agent == specialist
```

因此旧 Result 的 PASS、其他 Agent 的 PASS 或其他 Handoff 的 PASS 都不能结束当前任务。Specialist 重试产生新 `result_id` 后，旧验证自动失效。

## 组件职责

- **SupervisorAgent**：理解任务、生成 `SupervisorPlan`、确定必要 Specialist、路由、重试和结束；
- **ResearchAgent**：通过独立 ReAct Loop 调用 Tavily Search/Extract，输出带 Web Observation 证据的 `ResearchResult`；
- **MultiAgentCodeAgent**：执行结构化 Handoff，只接收 `context_refs` 指定的信息，输出 `CodeResult`；
- **VerificationGate**：选择验证策略，并强制检查 Handoff、Result 和 Verification 的 ID 关联；
- **ResearchResultVerifier**：检查 findings、query、来源数量以及 URL 与 Web Observation 的对应关系；
- **CodeEnvironmentVerifier**：检查本轮产物归属、固定命令结果和 Hybrid 来源引用；
- **HistoryStore**：保存未来 Agent 可复用的 Handoff、Result 和 Verification，不保存完整执行 Trace；
- **Retriever**：按 Profile 执行 exact → keyword → recent 检索；
- **ContextBuilder**：把任务、验收标准、Task Board 和相关 History 组装为 Base Context；
- **PromptAssembler**：根据 Agent 与 Phase 组装稳定规则、角色和阶段规则；
- **ToolSelector / ToolExposureGuard**：减少本轮暴露的 Tool Schema，并拒绝调用未暴露工具；
- **ContextMonitor**：统计完整模型调用输入和输出预留预算；
- **Base / Local Compressor**：分别压缩外部工作上下文与单次 Agent 局部交互；
- **NotepadStore**：按 task/session/agent/scope 筛选经过批准的长期事实；
- **FinalizationService**：在 FINISH Guard 通过后按 task_id/final_result_id 幂等持久化最终事实；
- **TaskBoard**：结构化跟踪多个 Todo 的 owner、attempts 和身份链；
- **TikiState**：整个 Workflow 唯一 canonical runtime state；
- **ExecutionHarness**：按 Exposure → Permission → Approval → Runtime 顺序执行安全管线；
- **Dispatcher**：底层参数规范化与 Python handler 调用；`dispatch()` 仅为 legacy/internal 兼容入口；
- **Workspace**：拒绝绝对路径和目录逃逸；
- **ModelClient**：隔离 OpenAI-compatible 模型协议，支持 DeepSeek 和本地 vLLM。

核心边界：

```text
Agent   = 决定做什么
Harness = 决定怎样执行
Gate    = 决定当前 Result 是否有足够证据
Supervisor = 决定验证之后往哪里走
Retriever = 决定找什么
ContextBuilder = 决定怎样组装 Base Context
PromptAssembler = 决定当前应该怎样工作
ToolSelector = 决定本轮向模型展示哪些工具
Tool Exposure Guard = 拒绝调用本轮未展示工具
Permission = 即使工具存在且已展示，当前是否允许执行
Approval = 是否授权当前 Scope 下的这个具体规范化 ToolCall
```

## Tool Isolation

三个角色使用不同 Registry：

```text
ResearchAgent
├── web_search
└── web_extract

CodeAgent
├── read_file / list_files / grep
├── write_file / edit_file
└── run_command

Code Environment Verifier
├── read_file / list_files / grep
└── 应用预先配置的检查命令
```

ResearchAgent 没有文件和命令工具。Verifier 的文件 Registry 没有 `write_file` 和 `edit_file`；验证命令由应用代码固定，不由 Verifier 模型动态生成。

## Handoff 与 Context Isolation

Specialist 之间不传递完整内部 messages：

```text
ResearchAgent internal messages
    ✗ 不进入 TikiState
    ✗ 不直接传给 CodeAgent

ResearchResult
    ✓ 结构化 findings
    ✓ 可追溯 sources
    ✓ 有界 Web Observations
    ✓ result_id / handoff_id
```

Handoff 的 `context_refs` 使用真实实体 ID，不再使用 `research_result`、`verification_report` 等符号名称：

```text
research result_id
failed code result_id
latest verification_id
```

任务、验收标准和当前 Todo 是 State 中的运行事实，由 ContextBuilder 根据 Profile 自动加入，不伪装成 History 引用。

Retriever 默认执行：

```text
exact context_refs
        ↓ 没有命中
keyword
        ↓ 仍没有命中
recent
```

Profile 可以配置少量 keyword/recent 补充检索。CodeAgent 和 Verifier 默认不在精确引用后追加旧同主题记录，避免最新 FAIL 与旧 PASS 同时进入修复上下文。

当前 Verification Gate 是规则与环境验证，直接消费 Handoff、Result 和 Workspace 证据，不为了接口统一强制生成 LLM Prompt。`VerifierProfile` 保留给未来需要模型输入的验证策略。

## Canonical TikiState

ReAct、Plan/Verify 和 Multi-Agent 工作流共享同一个 `TikiState` schema，不引入 `MultiAgentState`、`ContextState` 或运行时转换层。

v0.5 Context II 的主要状态分区：

```text
Task
├── task_id
├── task
└── session_id

Planning / Orchestration
├── supervisor_plan
├── required_specialists
├── supervisor_decision
├── current_agent
├── delegation_count
└── max_delegations

Collaboration / Verification
├── latest_handoff
├── task_board
├── specialist_results
├── specialist_verifications
└── verification_report

Runtime / Completion
├── workspace_id
├── history_cursor
├── runtime_checkpoint_id / revision
├── resume_request
├── trace_cursor
├── status
├── final_result
├── final_result_id
└── finalization_report
```

`specialist_results` 和 `specialist_verifications` 只保存每个 Specialist 的最新状态，不保存无限历史。

- `TaskBoard` 可以让同一个 Agent 拥有多个 Todo；每项 Todo 独立经历 `pending → in_progress → awaiting_verification → completed/failed`；
- `HistoryStore` 保留每次 Handoff、Result 和 Verification，旧尝试不会被最新状态覆盖；
- `history_cursor` 只是外部 History 的位置引用，不把 History 复制回 State；
- `recent_handoffs` 已从 State 移除；
- `recent_events` 仍是最多 50 条的应用展示缓存；工具执行审计已经写入持久化 Trace，Resume 从不依赖两者推断状态；
- `runtime_checkpoint_id` 避开 LangGraph 保留字段名 `checkpoint_id`，仅保存外部 Checkpoint 引用。

```text
State = 系统现在是什么状态
Trace = 系统之前发生过什么
History = 过去产生、未来可能检索的信息
Task Board = 当前任务做到哪里
Working Memory = 当前步骤所需信息集合
Base Context = ContextBuilder 为本轮 Agent 组装的输入
Prompt Bundle = 当前 Agent 与 Phase 的行为规则
Local Memory = 单次 Agent.run() 的 Summary + Recent ReAct Interactions
Candidate Model Call = 压缩前的完整候选调用
Prepared Model Call = 预算检查后真正发送给模型的调用
Notepad = 经过批准、以后不能忘记的长期事实
```

## Quick Start

要求 Python 3.13+ 和 [uv](https://docs.astral.sh/uv/)。

```powershell
git clone https://github.com/ZhuYaozong/TikiAgent.git
cd TikiAgent
uv sync --default-index https://pypi.org/simple
uv run --locked --default-index https://pypi.org/simple pytest
```

复制配置模板：

```powershell
Copy-Item .env.example .env
```

DeepSeek + Tavily 示例：

```dotenv
TIKI_LLM_API_KEY=your-model-api-key
TIKI_LLM_BASE_URL=https://api.deepseek.com
TIKI_LLM_MODEL=deepseek-chat

TIKI_SEARCH_PROVIDER=tavily
TIKI_TAVILY_API_KEY=tvly-your-key
TIKI_TAVILY_BASE_URL=https://api.tavily.com
```

`.env` 已被 Git 忽略。真实密钥不得写入 README、示例代码、测试或提交记录。

创建 Session 不需要模型配置；提交 CHAT/WORKFLOW 时才懒加载 OpenAI-compatible 客户端，路由到 ResearchAgent 时才读取 Tavily 配置：

```powershell
uv run --locked tikiagent --json new-session --workspace-id demo

uv run --locked tikiagent submit `
  --session-id <SESSION_ID> `
  --message "调研最近 Agent Framework 的变化并生成 comparison.html"

uv run --locked tikiagent status --session-id <SESSION_ID>
```

启动 Textual TUI：

```powershell
uv run --locked tikiagent-tui --data-dir .tiki --env-file .env

# 连接已有 Session，并从权威 Checkpoint 读取当前状态
uv run --locked tikiagent-tui --data-dir .tiki --session-id <SESSION_ID>
```

创建 Session 和查看本地状态不需要 API Key；真正提交 CHAT/WORKFLOW 时才会懒加载模型配置。

Approval 和未知副作用恢复都必须携带权威 Checkpoint 的 revision 以及绑定 ID：

```powershell
uv run --locked tikiagent resume `
  --session-id <SESSION_ID> `
  --request-id <APPROVAL_REQUEST_ID> `
  --expected-revision <REVISION> `
  --approve

uv run --locked tikiagent recover `
  --session-id <SESSION_ID> `
  --execution-id <EXECUTION_ID> `
  --expected-revision <REVISION> `
  --action confirmed_not_executed `
  --decided-by operator `
  --reason "已检查外部状态，handler 未运行"

uv run --locked tikiagent reconcile `
  --session-id <SESSION_ID> `
  --execution-id <EXECUTION_ID> `
  --expected-revision <REVISION> `
  --result-file .\reconcile-result.json
```

`confirmed_executed` 只会进入 `awaiting_reconcile`。`reconcile-result.json` 必须提供与当前 ToolCall 绑定的真实 `ToolResult`、操作者和 evidence，CLI 不会伪造成功结果。

本地 vLLM 只需替换模型配置：

```dotenv
TIKI_LLM_API_KEY=local-token
TIKI_LLM_BASE_URL=http://localhost:8000/v1
TIKI_LLM_MODEL=your-local-model
```

## 运行 Multi-Agent Demo

默认 Hybrid 任务：

```powershell
uv run --locked --default-index https://pypi.org/simple python examples/multi_agent_report.py
```

预期路径：

```text
Supervisor
→ ResearchAgent
→ Verification Gate
→ Supervisor
→ CodeAgent
→ Verification Gate
→ Supervisor
→ Finish
```

网页输出到：

```text
.tiki/multi-agent-workspace/comparison.html
```

也可以传入自定义任务：

```powershell
uv run --locked --default-index https://pypi.org/simple python examples/multi_agent_report.py `
  --task "创建一个 comparison.html，对比两种 Python 测试框架。"
```

Coding-only 任务不会路由 ResearchAgent，因此不会读取 Tavily Key，也不会初始化 Tavily Provider。

## Graph Routing 与 Agent-as-Tool

TikiAgent v0.4 主工作流使用 Graph Routing。为了独立展示另一种 Multi-Agent pattern，项目保留：

```powershell
uv run --locked --default-index https://pypi.org/simple python examples/agent_as_tool.py
```

区别：

```text
Graph Routing
SupervisorDecision → Conditional Edge → ResearchAgent Node

Agent-as-Tool
Supervisor ToolCall → call_research_agent → ResearchAgent.run()
```

`agent_as_tool.py` 不参与正式 `MultiAgentWorkflow`，避免两种 orchestration pattern 混用。

## Baselines

已有工作流继续作为架构演进的可运行 baseline：

```powershell
uv run python examples/react_file_repair.py
uv run python examples/langgraph_react.py
uv run python examples/plan_verify_repair.py
```

正式代码使用语义名称 `react_graph.py`、`plan_verify.py` 和 `multi_agent.py`，不会增加 `workflow_v4.py`、`workflow_final.py` 等版本化文件。当前版本不实现自动 Evaluation 框架，也不声明未经实验支持的量化收益。

## 项目结构

```text
src/tikiagent/
├── application/
│   ├── cli.py
│   ├── context_refs.py
│   ├── controller.py
│   ├── events.py
│   ├── harness_events.py
│   ├── models.py
│   ├── routing.py
│   ├── runtime.py
│   ├── session.py
│   └── workflow.py
├── agents/
│   ├── code.py
│   ├── planner.py
│   ├── react.py
│   ├── resumable.py
│   ├── research.py
│   ├── supervisor.py
│   └── verifier.py
├── context/
│   ├── builder.py
│   ├── history.py
│   ├── models.py
│   ├── profiles.py
│   ├── retriever.py
│   └── task_board.py
├── harness/
│   ├── approval.py
│   ├── checkpoint.py
│   ├── command_tools.py
│   ├── coordinator.py
│   ├── dispatcher.py
│   ├── execution.py
│   ├── file_tools.py
│   ├── guards.py
│   ├── models.py
│   ├── permission.py
│   ├── recovery.py
│   ├── registry.py
│   ├── trace.py
│   ├── web_tools.py
│   └── workspace.py
├── llm/
│   ├── config.py
│   ├── models.py
│   ├── openai_compatible.py
│   └── structured_output.py
├── tui/
│   ├── adapter.py
│   ├── app.py
│   ├── backend.py
│   ├── commands.py
│   ├── messages.py
│   ├── modals.py
│   ├── models.py
│   ├── sink.py
│   ├── styles.tcss
│   └── workspace.py
└── orchestration/
    ├── models.py
    ├── multi_agent.py
    ├── plan_verify.py
    ├── react_graph.py
    ├── state.py
    └── verification_gate.py
```

## Testing

自动测试不调用真实模型或 Tavily，使用 Fake Model、Fake Provider 和临时 Workspace 验证：

- Research-only、Coding-only 和 Hybrid Graph 路由；
- 每个 Specialist 后都经过 Verification Gate；
- Handoff、Result 和 Verification ID 关联；
- 旧 PASS 不能验证新 Result；
- Research 来源 allowlist 与 Observation provenance；
- Verifier FAIL 后返回 Supervisor；
- `max_delegations`、Agent `max_steps` 和 LangGraph `recursion_limit`；
- Registry 能力隔离；
- Base Context 与单次 ReAct messages 的两层上下文隔离；
- Candidate → Monitor → Compression → Prepared 调用生命周期；
- Tool Schema、Response Schema 与输出预留预算统计；
- Base Summary 替换旧 History，而不是与旧原文叠加；
- 多 ToolCall / ToolResult 的原子 Interaction 裁剪；
- 同一次运行切换 debugging 保留 LocalMemory，重新委派不继承；
- 动态 Prompt、Tool View 与 `tool_not_exposed` 运行时保护；
- Exposure 在 Permission 前执行，隐藏工具不会进入策略评估；
- ALLOW / ASK / DENY 对 handler 的真实 Enforce；
- Approval 的 task/session/workspace/参数绑定与一次性消费；
- ASK 不产生 ToolResult，也不能形成不完整 ReActInteraction；
- 结构化 argv 的测试命令、环境变更和未分类命令策略；
- `before_execute` 严格位于 Approval PASS 与 handler 之间；
- Checkpoint 同时保存 Workflow 与 ReAct 两层快照，并用 checksum 校验；
- CAS revision 拒绝重复 Resume，Trace 使用独立 sequence；
- 新进程经 Resume Entry Router 重入 Graph，而不是在 Graph 外续跑；
- JSONL History 在新进程中恢复记录、cursor 和幂等身份；
- 多 ToolCall 在中途 ASK 后按原顺序继续，并在完整配对后写 LocalMemory；
- `executing` 崩溃进入 `recovery_required`，不会自动重放；
- `confirmed_executed` 在人工提供真实 ReconcileResult 前保持阻塞；
- Session 仅保存 Checkpoint 引用，status/resume 从权威 Checkpoint 读取状态；
- Checkpoint 先持久化、Session 后绑定的暂停顺序；
- CHAT / WORKFLOW 路由与无工具 Chat 路径；
- 同 Session 显式 Result 引用可跨 Task、不可跨 Session；
- EventBus stream sequence、Secret 脱敏、截断和 Sink 故障隔离；
- Workflow/Harness 事实由各自 Adapter 发布，不由 Controller 反推；
- CLI 无模型配置创建 Session，以及真实 resume/recover/reconcile 参数入口；
- TUI Event → Adapter → ViewState 纯显示投影和 stream 顺序保护；
- Controller Worker → Textual Message → UI 主线程更新边界；
- Approval 防重复提交、Recovery → Reconcile 和执行期退出提示；
- Session 连接、多轮 transcript 与只读 Workspace Tree；
- Notepad 审批、作用域过滤、幂等写入和 Markdown 重载；
- FINISH Guard 后 Finalization 以及节点重放幂等性；
- History Store 幂等写入、作用域和冲突检查；
- exact → keyword → recent 以及 Profile 补充策略；
- Supervisor 全局 Task Board 与 Specialist Todo 隔离；
- 同一个 Agent 多个 Todo 的独立 Result/Verification 生命周期；
- Retry 只获得显式引用的新 FAIL，不混入旧 PASS；
- 有界 `recent_events`；
- Workspace Boundary、工具错误、命令超时和输出截断；
- v0.1～v0.4 全量回归。

## 当前限制

- Command Runtime 的 Workspace `cwd` 限制不是操作系统 Sandbox，子进程仍可能主动访问 Workspace 外资源；
- `InMemoryApprovalLedger` 仍只负责当前进程校验；跨进程执行事实以 Checkpoint 为准；
- v0.1～v0.5 Baseline 仍保留 legacy Dispatcher，正式 Multi-Agent 示例使用 Harness Runtime；
- `InMemoryHistoryStore` 仍可用于短测试；需要 Resume 的 Workflow 强制使用 `JsonlHistoryStore`；
- JSONL Checkpoint/History 适合单机 v1，不提供多进程文件锁或分布式 exactly-once；
- Trace 是 best effort，崩溃前最后几条事件可能缺失，但不会改变 Checkpoint 恢复语义；
- `recent_events` 仍是 Graph 内的有界调试缓存；Application Event Stream 与 Trace 独立，不从 Trace 推断实时语义；
- Runtime 的 Code Environment Verifier 暂时沿用主 Hybrid Demo 的 `comparison.html` 契约；通用动态验收配置尚未实现；
- TUI 重启后通过 Session 与权威 Checkpoint 恢复当前状态，不从 Trace 重放完整历史事件时间线；
- Python 线程 Worker 无法安全强杀正在运行的同步 handler，因此执行期退出只提示恢复风险，不承诺取消 Workflow；
- Workspace Tree 第一版只读，不提供文件内容预览或编辑；
- 默认 `InMemoryNotepadStore` 不跨进程；应用可以显式使用 `.tiki/NOTEPAD.md` 的 `MarkdownNotepadStore`；
- 当前 Token 统计是字符近似值，不是供应商精确 Tokenizer；
- 当前 Compressor 是确定性规则实现，尚未实现 LLM Structured Summary；
- Recent Window 按完整 Interaction 数量裁剪，尚未升级为 token-aware window；
- Tool Selector 与 Exposure Guard 仍不等于 Permission；v0.6a1 已把三者作为独立执行层；
- Research rule verification 能证明来源来自真实 Web Observation，不能自动证明来源内容绝对真实；
- Supervisor 的语义规划依赖模型质量，关键 FINISH 与身份关联由程序规则保护；
- OpenAI-compatible 后端共享协议格式，但不同模型的 Tool Calling 能力仍可能不同。

## Roadmap

- [x] v0.1 ReAct Agent、Tool Registry、Dispatcher、Workspace；
- [x] v0.2 LangGraph 与 canonical TikiState；
- [x] v0.3 Plan → Execute → Verify；
- [x] v0.4 Supervisor、ResearchAgent、CodeAgent、Handoff、Verification Gate；
- [x] v0.5 Context I：History、Retriever、Task Board、Context Profiles、Context Builder；
- [x] v0.5 Context II：Monitor、Compressor、Notepad、动态 Prompt/Tool、Finalization；
- [x] v0.6a1 Gate / Enforce / Isolate：Permission、Approval 与安全执行管线；
- [x] v0.6a2 Persist / Observe：双快照 Checkpoint、Graph Resume、Trace、持久化 History 与 Agent Runtime；
- [x] v0.7 Application：Session、Turn、Intent Router、Event Stream、CLI 与恢复入口；
- [x] v0.8 Textual TUI：实时事件、多轮 Session、审批/恢复 Modal、只读 Workspace Tree；
- [ ] v0.9 Demo Validation：Research / Coding / Hybrid 三个主 Demo 与 Trace 总结；
- [ ] v1.0 README、架构材料、演示录制与面试答辩。

## v1 目标 Demo

1. Research：搜索重要 AI Agent 新闻并总结可追溯来源；
2. Coding：创建 Python 项目并运行测试；
3. Multi-Agent：调研 Agent Framework 的变化，并根据验证通过的调研结果生成对比网页。
