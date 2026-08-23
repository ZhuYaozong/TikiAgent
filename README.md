# TikiAgent

TikiAgent 是一个渐进式构建的 **Multi-Agent Task Execution System**。它使用 Supervisor 根据任务动态调度 ResearchAgent 和 CodeAgent，通过统一 Verification Gate 验证每次 Specialist 交付，再由 Supervisor 决定继续委派或结束。

当前版本为 **v0.4 Multi-Agent + Verification Gate**。

## v0.4 架构

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
- **TikiState**：整个 Workflow 唯一 canonical runtime state；
- **Dispatcher**：验证工具名称与参数、执行 Python Tool、统一返回 `ToolResult`；
- **Workspace**：拒绝绝对路径和目录逃逸；
- **ModelClient**：隔离 OpenAI-compatible 模型协议，支持 DeepSeek 和本地 vLLM。

核心边界：

```text
Agent   = 决定做什么
Harness = 决定怎样执行
Gate    = 决定当前 Result 是否有足够证据
Supervisor = 决定验证之后往哪里走
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

CodeAgent 只获得 Handoff 中 `context_refs` 指定的数据，例如：

```text
acceptance_criteria
research_result
verification_report
```

这为下一阶段 Context Builder 和 Retriever 保留了明确接入点。

## Canonical TikiState

ReAct、Plan/Verify 和 Multi-Agent 工作流共享同一个 `TikiState` schema，不引入 `MultiAgentState`、`ContextState` 或运行时转换层。

v0.4 的主要状态分区：

```text
Task
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
├── specialist_results
├── specialist_verifications
└── verification_report

Runtime / Completion
├── workspace_id
├── status
└── final_result
```

`specialist_results` 和 `specialist_verifications` 只保存每个 Specialist 的最新状态，不保存无限历史。

`recent_events` 和 `recent_handoffs` 是 v0.4 的有界过渡字段：

- `recent_events` 最多保留 50 条，Harness Engineering 阶段迁移到 Trace；
- `recent_handoffs` 最多保留 20 条，Context Engine 阶段将完整历史迁移到 History Store。

```text
State = 系统现在是什么状态
Trace = 系统之前发生过什么
History = 过去产生、未来可能检索的信息
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

已有工作流继续作为后续 Evaluation 的可运行 baseline：

```powershell
uv run python examples/react_file_repair.py
uv run python examples/langgraph_react.py
uv run python examples/plan_verify_repair.py
```

正式代码使用语义名称 `react_graph.py`、`plan_verify.py` 和 `multi_agent.py`，不会增加 `workflow_v4.py`、`workflow_final.py` 等版本化文件。Git 历史和后续 `benchmarks/baselines/` 负责保存架构演进与消融基线。

## 项目结构

```text
src/tikiagent/
├── agents/
│   ├── code.py
│   ├── planner.py
│   ├── react.py
│   ├── research.py
│   ├── supervisor.py
│   └── verifier.py
├── harness/
│   ├── command_tools.py
│   ├── dispatcher.py
│   ├── file_tools.py
│   ├── models.py
│   ├── registry.py
│   ├── web_tools.py
│   └── workspace.py
├── llm/
│   ├── config.py
│   ├── models.py
│   ├── openai_compatible.py
│   └── structured_output.py
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
- 有界 `recent_events` / `recent_handoffs`；
- Workspace Boundary、工具错误、命令超时和输出截断；
- v0.1～v0.3 全量回归。

## 当前限制

- Command Runtime 的 Workspace `cwd` 限制不是操作系统 Sandbox，子进程仍可能主动访问 Workspace 外资源；
- 尚未实现 Permission、Human Approval、Checkpoint、Resume 和正式 Trace；
- 尚未实现 History Store、Retriever、Context Builder 和 Compressor；
- Research rule verification 能证明来源来自真实 Web Observation，不能自动证明来源内容绝对真实；
- Supervisor 的语义规划依赖模型质量，关键 FINISH 与身份关联由程序规则保护；
- OpenAI-compatible 后端共享协议格式，但不同模型的 Tool Calling 能力仍可能不同。

## Roadmap

- [x] v0.1 ReAct Agent、Tool Registry、Dispatcher、Workspace；
- [x] v0.2 LangGraph 与 canonical TikiState；
- [x] v0.3 Plan → Execute → Verify；
- [x] v0.4 Supervisor、ResearchAgent、CodeAgent、Handoff、Verification Gate；
- [ ] v0.5 History、Retriever、Context Builder、Monitor、Compressor、Notepad；
- [ ] v0.6 Permission、Approval、Checkpoint、Resume、Trace；
- [ ] Session、CLI、Event Stream 与 Evaluation；
- [ ] Single-Agent / Plan-Verify / Multi-Agent / Context Engine 消融实验。

## v1 目标 Demo

1. Research：搜索重要 AI Agent 新闻并总结可追溯来源；
2. Coding：创建 Python 项目并运行测试；
3. Multi-Agent：调研 Agent Framework 的变化，并根据验证通过的调研结果生成对比网页。
