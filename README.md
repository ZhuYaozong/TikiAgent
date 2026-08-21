# TikiAgent

TikiAgent 是一个渐进式构建的 **Multi-Agent Task Execution System**。目标是根据任务动态调度不同的 Specialist Agent，并通过验证闭环、上下文工程和安全执行环境完成 Research、Coding 与复合任务。

当前版本为 **v0.3 Plan / Verify Loop**：在 ReAct Actor 和结构化 `TikiState` 之上，新增 Planner、只读 Verifier 与失败重试闭环。

## v0.3 架构

```text
User Task
    ↓
START → Planner ── EXECUTE / RETRY ──→ ReAct Code Actor
          ▲                                  │
          │                                  ▼
          └──────── VerificationReport ← Verifier
          │
          └── FINISH / STOP ───────────────→ END

Planner: StructuredModelClient → Plan / PlannerDecision
Actor:   ModelClient → ReAct → Dispatcher → Workspace
Verifier: fixed checks → read-only Registry → CommandResult
```

当前职责边界：

- **ModelClient**：把内部 Tool Schema 和模型响应适配为 OpenAI-compatible Chat Completions 协议；
- **StructuredModelClient**：使用 JSON Schema 提示和 Pydantic 本地校验生成结构化控制数据；
- **ReActAgent**：保留为手写循环 baseline；
- **PlannerAgent**：生成 Plan，并根据 VerificationReport 决定 RETRY、FINISH 或 STOP；
- **ReActCodeActor**：复用 ReActAgent 执行计划，只向外传递 ActorResult；
- **EnvironmentVerifier**：执行应用预先配置的环境检查，不修改文件、不决定路由；
- **TikiState**：保存 task、计划、验收标准、ActorResult、VerificationReport、限制与完成状态；
- **ReActWorkflow**：使用 Actor Node、Tools Node 和 Conditional Edge 显式编排循环；
- **PlanVerifyWorkflow**：强制 Verifier 回到 Planner，并限制失败报告直接 FINISH；
- **Dispatcher**：验证工具名称和参数、执行 Python 工具、返回结构化错误；
- **Workspace**：拒绝绝对路径和目录逃逸；
- **File Tools**：提供 `read_file`、`write_file`、`edit_file`、`list_files` 和 `grep`；
- **Command Runtime**：使用参数列表和 `shell=False` 启动进程，限制 `cwd`、超时与返回给模型的输出长度。

核心原则：

```text
Agent   = 决定做什么
Harness = 决定怎样安全、可观察地执行
```

## Quick Start

环境要求：Python 3.13+、[uv](https://docs.astral.sh/uv/)。

```powershell
git clone https://github.com/ZhuYaozong/TikiAgent.git
cd TikiAgent
uv sync
uv run pytest
```

复制配置模板：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，配置任意 OpenAI-compatible 后端。DeepSeek 示例：

```dotenv
TIKI_LLM_API_KEY=your-api-key
TIKI_LLM_BASE_URL=https://api.deepseek.com
TIKI_LLM_MODEL=deepseek-chat
```

`.env` 已被 Git 忽略，不要把真实密钥写入 README、示例代码或提交记录。

运行手写 ReAct baseline：

```powershell
uv run python examples/react_file_repair.py
```

运行 LangGraph + TikiState 版本：

```powershell
uv run python examples/langgraph_react.py
```

运行 Plan → Execute → Verify 版本：

```powershell
uv run python examples/plan_verify_repair.py
```

切换到本地 vLLM 时只需修改配置，Agent 和 Harness 不变：

```dotenv
TIKI_LLM_API_KEY=local-token
TIKI_LLM_BASE_URL=http://localhost:8000/v1
TIKI_LLM_MODEL=your-local-model
```

## 故障处理

工具失败不会直接让 Agent 崩溃。Dispatcher 会把故障转换为结构化 `ToolResult`：

```json
{
  "ok": false,
  "error": {
    "code": "file_not_found",
    "message": "文件不存在",
    "details": {"path": "missing.txt"}
  }
}
```

该结果作为 Observation 返回模型，由 Agent 决定重试、调整参数或结束。Dispatcher 本身不进行语义决策。

命令执行使用两层状态：

```text
ToolResult.ok
= Harness 是否正常处理了工具调用

CommandResult.exit_code / timed_out
= 子进程是否成功完成
```

因此非零退出码和超时仍会返回 `ToolResult.ok=true`，保留 stdout、stderr 和运行时间供 Agent 分析。命令成功的依据是 `timed_out=false` 且 `exit_code=0`，不能根据 stderr 是否为空判断。

示例中的 Python 测试使用 `python -B -m unittest ...`。在极短时间内把源码替换为相同长度的文本时，禁用字节码写入可以避免基线测试生成的时间戳型 `.pyc` 被复验进程误用。

## 项目结构

```text
src/tikiagent/
├── agents/
│   ├── code.py
│   ├── planner.py
│   ├── react.py
│   └── verifier.py
├── harness/
│   ├── dispatcher.py
│   ├── command_tools.py
│   ├── file_tools.py
│   ├── models.py
│   ├── registry.py
│   └── workspace.py
├── llm/
│   ├── config.py
│   ├── models.py
│   ├── openai_compatible.py
│   └── structured_output.py
└── orchestration/
    ├── models.py
    ├── plan_verify.py
    ├── react_graph.py
    └── state.py
```

测试不调用外部模型 API，使用确定性 Fake Model / Fake Agent 验证手写 Agent 与 LangGraph。当前覆盖 Structured Output 修正、Planner 决策保护、Verifier 工具隔离、成功闭环、失败重试、`max_attempts`、Reducer、工具错误、路径逃逸、命令非零退出、超时和输出截断。

## State 边界

```text
TikiState
= 当前 workflow 的结构化运行快照

messages
= 当前 Agent 调用模型所需的工作消息

Plan / ActorResult / VerificationReport
= Control Plane 节点之间的结构化 Handoff

Workspace
= 文件和命令实际作用的外部环境

History
= 尚未实现的完整过去信息存储
```

`TikiState` 只保存 `workspace_id`，不会把 Workspace 文件内容放进状态。`messages` 和 `tool_results` 使用 Reducer 追加；`pending_tool_calls`、`status` 与计数器使用覆盖更新。

PlanVerifyWorkflow 不把 ReActCodeActor 的完整内部 messages 交给 Planner 或 Verifier，只传递 `ActorResult` 和环境证据，避免角色上下文直接混合。

`max_steps` 是业务层的模型决策次数上限；`recursion_limit` 是 LangGraph 对整张图节点执行次数的基础设施保护。一次工具调用会经过 Actor 和 Tools 两个节点，因此两者不能按相同数值理解。

在 PlanVerifyWorkflow 中，`max_attempts` 限制外层 Actor → Verifier 执行轮数；Actor 自己的 `max_steps` 限制单轮 ReAct 模型决策；外层 `recursion_limit` 保护 Planner / Actor / Verifier 节点循环。

## 当前限制

- Command Runtime 的 Workspace `cwd` 限制不是操作系统 Sandbox，子进程仍可能主动访问 Workspace 外部资源；
- 当前输出限制在进程结束后截断返回内容，尚未实现流式输出背压；
- 尚未实现命令 allowlist、Permission、Human Approval、Checkpoint 和 Trace，不应在不受信任环境中开放任意命令；
- 当前只有 Planner + Code Actor + Verifier，尚未实现 Supervisor、ResearchAgent 和真正的 Multi-Agent Handoff；
- Verifier 的文件 Registry 不包含写工具，但 Command Runtime 还不是操作系统 Sandbox；当前验证命令必须由应用代码固定配置；
- OpenAI-compatible 后端共享协议格式，但不同模型的工具调用能力仍可能不同。

## v1 目标架构

```text
User
  ↓
Supervisor ── Plan / Route / Delegate
  ├── ResearchAgent
  └── CodeAgent
          ↓
       Verifier
          ↓
      Supervisor
      ↙        ↘
 Continue     Finish
```

最终系统分为 Control Plane、Context Plane 和 Execution Plane。v0.1 建立 Execution Plane 与 ReAct baseline；v0.2 建立结构化状态和 Graph 基础；v0.3 建立第一个带环境证据的控制闭环。

## Roadmap

- [x] 初始化 Python 项目和测试；
- [x] OpenAI-compatible `ModelClient`；
- [x] Tool Registry、Dispatcher 与结构化 ToolResult；
- [x] Workspace Boundary 与文件工具；
- [x] 真实模型 ReAct Agent 和 `max_steps`；
- [x] Command Runtime、timeout、cwd、output limit 与测试闭环；
- [ ] Permission、Approval、更强的进程隔离、Checkpoint 与 Trace；
- [x] LangGraph 与结构化 TikiState；
- [x] Plan → Execute → Verify；
- [ ] Supervisor、ResearchAgent 与 CodeAgent；
- [ ] History、Retriever、Context Builder 与 Compressor；
- [ ] Session、CLI、Event Stream 与 Evaluation。

## v1 目标 Demo

1. Research：搜索重要 AI Agent 新闻并总结来源；
2. Coding：创建 Python 项目并运行测试；
3. Multi-Agent：调研 Agent Framework 的变化，并根据调研结果生成对比网页。
