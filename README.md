<p align="center">
  <img src="docs/assets/tikiagent-logo.svg" width="720" alt="TikiAgent Logo">
</p>

<h1 align="center">TikiAgent</h1>

<p align="center">
  A multi-agent task execution system for research, coding, and hybrid workflows.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.0.0-16884A" alt="Version 1.0.0">
  <img src="https://img.shields.io/badge/python-3.13%2B-3776AB" alt="Python 3.13+">
  <img src="https://img.shields.io/badge/orchestration-LangGraph-6B4EFF" alt="LangGraph">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-F6D55C" alt="MIT License"></a>
</p>

TikiAgent 使用 Supervisor 动态规划和委派任务，由 ResearchAgent 与 CodeAgent 完成专业工作，再通过 Verification Gate 验证交付结果。独立的 Context Engine 控制每位 Agent 看见什么，Execution Harness 控制工具如何安全、可恢复地执行，Application Plane 提供 Session、CLI、Event Stream 和 Conversation-first Textual TUI。

> 当前稳定版本：**v1.0.0**。项目不把 Demo Validation 描述为统计 Evaluation，也不声明未经实验支持的 Multi-Agent 性能优势。

## Features

| 能力 | 说明 |
|---|---|
| Multi-Agent orchestration | 工具型 Supervisor 创建带依赖的多 Todo 计划，自主检索、委派、调整或停止 |
| Conditional verification | 必经基础 Gate，复杂任务条件触发独立 VerifierAgent；Supervisor 决定验收，保留证据与身份边界 |
| Context engineering | 稳定提示词前置，History／Local 两层 LLM 摘要，近期完整交互与 token 预算保护 |
| Execution harness | Tool Exposure、Permission、Approval、Workspace、Timeout、Checkpoint 与 Trace |
| Recovery semantics | Approval 暂停、Checkpoint Resume、未知副作用 Recovery/Reconcile |
| OpenAI-compatible backend | 可连接 DeepSeek 官方 API 或本地 vLLM OpenAI-compatible endpoint |
| Web research | Tavily Search/Extract、显式授权历史复用，保留逐条结论引用、来源日期与证据类型 |
| Application interfaces | CLI、Event Stream、三类冻结 Demo 与 Textual 交互终端；工具卡片原地更新、只读任务进度与逐项审核详情 |

## Architecture

```text
                         User / CLI / TUI
                                │
                                ▼
                        Application Plane
                  Session · Turn · Router · Events
                                │
                                ▼
                           Supervisor
                    Plan · Route · Delegate
                         ╱             ╲
                        ▼               ▼
                ResearchAgent       CodeAgent
                        ╲               ╱
                         ▼             ▼
                      Verification Gate
                  基础检查 · 条件独立审核
                                │
                                ▼
                           Supervisor
                         Continue / Finish

     Context Plane                              Execution Plane
 State · Task Board · History              Exposure · Permission
 Retriever · Context Builder               Approval · Workspace
 Monitor · Compressor · Notepad            Checkpoint · Trace
```

核心控制流：

```text
Supervisor Decision
        ↓
Structured Handoff
        ↓
Specialist Result
        ↓
Verification Gate(result_id + handoff_id + todo_id)
        ↓ 基础检查 / 需要时独立 VerifierAgent
        ↓
Supervisor Review: Accept / Accept with limitations / Request changes / Stop
        ↓
Supervisor FINISH / RETRY / DELEGATE
```

完整设计见 [Architecture](docs/architecture.md) 和 [Design Decisions](docs/design-decisions.md)。

### Verification & Capabilities

ResearchAgent 仅使用 Tavily 搜索与提取网页；本地文件、Python 环境和依赖任务交给 CodeAgent。Supervisor 为每个 Todo 声明所需能力和逐项验收条件，委派后不能通过删除标准绕过失败。

ResearchAgent 可以直接提取已知文章，无须先重复搜索，也能复用 Supervisor 显式引用、Application 已授权的同 Session 调研结果。搜索摘录、实际提取的正文和历史来源分别标记；每条结论使用 `source_id` 对应真实来源，最终回答显示引用关系。`published_date` 只保留工具返回的字段，缺失时保持未知；正文中的日期作为原文证据保留，不从 URL 推断。Tavily 并非全网热度排名服务，无法满足的日期、数量或排名要求应明确披露，不能虚构完成。

正式委派的 Specialist 和 Verifier 只接收当前 Todo 与其冻结验收项，全局目标仅作为背景，Supervisor 保留全局 TaskBoard。新任务的参考时间包含时区，随已有 Checkpoint 保存；恢复不替换为当天日期，旧任务未保存的时间保持未知。证据目录、引用和历史授权边界详见 [研究委派与证据链](docs/research-evidence.md)。

常规搜索、只读调查、依赖安装和简单文件创建默认采用 `basic`：Gate 不调用 LLM、不运行额外命令，只检查结果身份、来源追溯、实际产物及 Workspace 边界。复杂任务采用 `independent`：跨任务代码交付、同时写入文件与执行命令的复合代码 Todo 会自动升级；其他需要独立审查的任务由 Supervisor 在规划时明确选择并给出理由。审核级别在委派后冻结，随 Todo/Handoff/Checkpoint 保存；缺少新字段的旧快照保留独立审核要求，不静默降级。

需要独立审核时，VerifierAgent 使用独立上下文和受控工具逐项读取证据，报告符合性、缺陷与不确定性。报告区分 `checks_only`（仅基础检查）、`assessed`（已有逐项审核）与 `not_performed`（审核未完成）；`passed` 在基础路径只表示机械检查满足，不能证明所有语义条件成立。两条路径都进入 `awaiting_review`，由 Supervisor 根据原始要求和交付通过 `review_result` 决定接受、带限制接受、补做或停止；不自动完成 Todo。

Verification Gate 保留硬约束：最新 `result_id/handoff_id/todo_id/verification_id` 绑定、来源可追溯和产物边界；独立审核报告还必须完整覆盖验收条件，单项 passed 必须有有效的结构化 `evidence_refs`，不能仅在 reason 中提及证据 ID。Supervisor 不能覆盖身份错配、伪造证据或未形成交付的权限阻塞，也不能接受不存在的交付文件。FINISH 要求每项最新结果已有匹配的接受决定，不要求审核意见全部 PASS。最终回答和 TUI 明确区分“基础检查完成”与“独立审核通过”，带限制接受不会隐去已知缺口。

独立验证取证达到预算后，保留一次仅提交报告的收尾机会，不增加取证次数。报告格式错误在当前有界审核循环内返回修正反馈；未取得合法报告或审核服务失败时明确返回审核未完成，禁止仅因审核失败而重跑 Research/Code。Supervisor 可对已有真实交付明确带限制接受并披露审核缺失，但不能伪造独立审核通过；无交付、权限或身份阻塞仍不能放行。

Python 环境任务可查询实际解释器与发行包版本、检查导入；安装仍必须走原来的审批/Checkpoint 链。已经满足要求的依赖不需要重复安装或创建无关测试。验证工具的隔离子进程、临时测试副本**不是强沙箱**，请只运行可信代码。详见 [验证契约与边界](docs/verification-agent.md)。

## Quick Start

### Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- OpenAI-compatible 模型 API
- Tavily API Key（仅 Research/Hybrid 任务需要）

### Install

```powershell
git clone https://github.com/ZhuYaozong/TikiAgent.git
cd TikiAgent
uv sync --locked
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
TIKI_LLM_API_KEY=your-model-api-key
TIKI_LLM_BASE_URL=https://api.deepseek.com
TIKI_LLM_MODEL=deepseek-flash
TIKI_LLM_API_STYLE=deepseek
TIKI_LLM_TIMEOUT_SECONDS=60
TIKI_LLM_MAX_RETRIES=1
TIKI_LLM_CONTEXT_LIMIT=131072
TIKI_LLM_MAX_OUTPUT_TOKENS=2000

TIKI_SEARCH_PROVIDER=tavily
TIKI_TAVILY_API_KEY=tvly-your-key
TIKI_TAVILY_BASE_URL=https://api.tavily.com
```

`.env` 已被 Git 忽略。不要把真实 API Key 写入 README、示例代码或提交记录。

模型请求默认超时为 60 秒、最多重试 1 次，可用以上两个可选配置调整。模型服务的 `insufficient_quota` 错误需要在服务控制台处理额度；修改项目步数不会解决配额不足。

ResearchAgent 每次委派默认最多 16 轮、8 次搜索和 10 次网页提取；整个任务最多 40 次联网请求，可通过 `.env.example` 中的 `TIKI_BUDGET_RESEARCH_*` 和 `TIKI_BUDGET_TASK_WEB_TOOLS` 调整。研究结果总述最多 2000 字符、结论最多 12 条且每条最多 800 字符；达到工作上限后仍保留一次总结，不放开重复请求保护。最终对话回答另有 7600 字符保护，因此研究结果上限不等于终端展示长度。配置修改后需重启程序；已有任务的持久化额度不会被扩容，新任务使用新额度。详见 [执行预算与进展策略](docs/execution-policy.md)。

`TIKI_LLM_CONTEXT_LIMIT` 是应用主动使用的总窗口，不是供应商最大能力。正式应用通过 `TIKI_OUTPUT_<STAGE>` 分阶段预留输出；`TIKI_LLM_MAX_OUTPUT_TOKENS` 只作为未分阶段客户端的默认值。上下文接近预算时会额外调用模型总结旧历史或旧交互，原始任务约束与 TaskBoard 不参与摘要。摘要失败不会覆盖旧消息，仍超限时明确停止。

DeepSeek 使用分阶段思考强度；`TIKI_LLM_API_STYLE=auto` 只自动识别官方 `api.deepseek.com`。连接 vLLM 或其他兼容接口时设置 `openai`，不发送 DeepSeek 专用参数。中转站只有明确支持这些参数时才使用 `deepseek`。

Supervisor 通过 `update_plan`、`read_history`、`delegate_task`、`finish_task`、`stop_task` 编排任务，实际执行仍由 Graph、Specialist 和 Verification Gate 完成。每个 Todo 独立关联结果和验证，审批恢复会继续原来的待完成委派。详见 [Supervisor 与上下文设计](docs/supervisor-context.md)。

### Start the TUI

```powershell
uv run --locked tikiagent-tui --data-dir .tiki --env-file .env
```

进入界面后直接输入任务，例如：

```text
搜索近期重要的 AI Agent 新闻并总结来源
```

或：

```text
调研近期 Agent Framework 的重要变化，并根据结果生成 comparison.html
```

TUI 支持多轮 Session、Markdown 最终回答、实时 Agent/Tool/Verification Feed、Approval、Recovery 和只读 Workspace Tree。详细命令见 [Textual TUI](docs/tui.md)。

需要人工审批时，弹窗显示本次工具的具体参数、工作目录、超时和审批原因。长参数可滚动查看，敏感值会遮蔽；批准仅针对当前一次执行。重新连接 Session 后，审批详情从权威 Checkpoint 重新读取。

## Demo Validation

项目冻结了三个可重复的产品场景：

```powershell
# 只查看任务、Agent 和验收条件，不调用外部 API
uv run --locked tikiagent-demo hybrid --dry-run --json

# 真实运行
uv run --locked tikiagent-demo research
uv run --locked tikiagent-demo coding
uv run --locked tikiagent-demo hybrid
```

| 场景 | 路由 | 交付 |
|---|---|---|
| Research | Supervisor → ResearchAgent → Verification | 带来源 URL 的调研总结 |
| Coding | Supervisor → CodeAgent → Verification | 代码、文件与自动化测试结果 |
| Hybrid | ResearchAgent → CodeAgent → Verification | 基于调研证据生成的对比网页 |

每次真实 Demo 会在 `.tiki-demo/demo-runs/<RUN_ID>/` 保存 Application Timeline、Harness Trace 摘要、最终结果和产物引用。完整说明见 [Demo Validation](docs/demos.md)。

## CLI

创建 Session：

```powershell
uv run --locked tikiagent --data-dir .tiki --env-file .env `
  new-session --workspace-id demo
```

提交任务：

```powershell
uv run --locked tikiagent --data-dir .tiki --env-file .env `
  submit --session-id <SESSION_ID> `
  --message "创建一个包含 unittest 的 Python 计算器项目"
```

查看权威运行状态：

```powershell
uv run --locked tikiagent --data-dir .tiki `
  status --session-id <SESSION_ID>
```

CLI 还提供 `resume`、`recover` 和 `reconcile`。这些入口读取 Session 引用的权威 Checkpoint，不从 UI 状态或 Trace 猜测恢复位置。

## Execution Safety

正式工具执行路径：

```text
Raw ToolCall
    ↓ basic validation
Tool Exposure Guard
    ↓ argument validation + canonicalization
    ↓
Permission: ALLOW / ASK / DENY
    ↓
Approval Scope + Fingerprint
    ↓
Checkpoint(executing)
    ↓
Workspace / Runtime Enforcement
    ↓
ToolResult + Trace
```

关键保证：

- 未暴露给当前 Agent 的工具不能通过伪造 ToolCall 执行；
- ASK 是独立 `awaiting_approval` 状态，不伪造成失败 ToolResult；
- Approval 绑定 task、session、workspace、规范化参数和 fingerprint；
- ToolCall 与 ToolResult 在 Local Memory 中保持原子配对；
- Checkpoint 是 Resume source of truth，Trace 只用于审计；
- `executing` 状态崩溃后禁止自动重放，必须人工 Recovery/Reconcile；
- Workspace Boundary、Timeout 和 Output Limit 在模型决策之外机械执行。

> Workspace Boundary 不是操作系统级 Sandbox。不要把 v1.0 当作不可信代码的强隔离容器。

## Project Structure

```text
src/tikiagent/
├── agents/          # Supervisor、ResearchAgent、CodeAgent 的角色策略
├── runtime/         # 单 Agent ReAct 循环、暂停与恢复
├── orchestration/   # 多 Agent Workflow、State、Handoff 与完成条件
├── verification/    # 统一验证入口及 Research / Code / Artifact 验证
├── context/         # 上下文组装，memory/ 信息源，compression/ 预算与压缩
├── tools/           # 工具协议、Registry、Dispatcher 与工具实现
├── harness/         # 执行强制边界，permissions/ 审批，persistence/ 恢复与审计
├── providers/       # llm/ 模型适配与 search/ 搜索服务适配
├── application/     # Session、Intent Router、Controller、Events 与应用装配
├── interfaces/      # CLI 与 tui/ Textual 界面、纯显示投影
├── baselines/       # 独立保留的 ReAct Graph、Plan/Verify 基线
└── demo/            # Research / Coding / Hybrid 场景与结果收集

examples/            # ReAct → LangGraph → Plan/Verify → Multi-Agent 基线
tests/               # 离线自动化测试
docs/                # 架构、设计决策、TUI、Demo 与演进说明
```

各目录的入口文件、依赖边界与 Python 导入迁移说明见 [模块导航](docs/module-layout.md)。

## Design Principles

| 边界 | 定义 |
|---|---|
| Agent vs Harness | Agent 决定做什么；Harness 决定如何执行 |
| Retriever vs Context Builder | Retriever 找相关信息；Builder 组装 Agent Context |
| State vs History | State 保存当前 Workflow 事实；History 保存未来可检索信息 |
| Memory vs Checkpoint | Memory 支持当前工作；Checkpoint 支持崩溃恢复 |
| History vs Trace | History 面向未来 Agent；Trace 面向执行审计 |
| Tool Selection vs Permission | Selection 控制展示；Permission 控制是否允许执行 |
| Verifier vs Supervisor | Verifier 提供证据；Supervisor 决定下一条控制边 |

## Testing

自动测试不调用真实模型或 Tavily，使用 Fake Model、Fake Provider 和临时 Workspace：

```powershell
uv run --locked pytest -q
uv run --locked python -m compileall -q src tests
```

当前覆盖包括 Agent 路由、Result/Verification 身份链、Context Compression、Tool Exposure、Permission/Approval、Checkpoint/Resume、Recovery/Reconcile、Session、Event Stream、Artifact Verification、TUI 投影和三类 Demo 生命周期。

上下文只加载有界的 Result/Verification 视图：保留身份关联、逐项验收结论、失败分类和证据引用，完整正文留在 History 按需读取。压缩不能覆盖当前任务、冻结验收条件、TaskBoard 或控制事实。模型截断或工具参数不完整时，最多重新生成一次；本批不完整调用不会执行。

运行失败会保存关联当前 Turn 的失败回复，CLI 返回非零状态，TUI 显示失败。下轮聊天使用逐 Turn 的最新回复，失败、暂停及未记录结果的旧任务不会被当成新指令自动续跑。失败不表示已回滚文件或外部副作用；恢复仍以权威 Checkpoint 为准，不依赖 Trace 猜测执行状态。

只读文件调查使用 `inspection` 验收，基础路径检查实际执行证据，无需创建报告；需要独立审查才由 Verifier 复读文件或目录。创建/修改文件使用 `artifact` 验收，继续检查实际交付物。模型步骤与工具调用分别计数；正式 CodeAgent 默认每次最多 32 次工具调用、每任务最多 64 次，相同工具参数累计失败 2 次后阻止继续执行。预算包含被拒绝和参数错误的调用，暂停/恢复不会重置已消耗次数。

TUI 中输入 `/paths` 可在本地查看当前 Session 的 Workspace、History/Handoff、Trace 和 Checkpoint 位置。此命令只更新显示，不提交给模型，也不写入任务 History。

### 有界执行与最终收尾

正式 Code、Research、Supervisor、Verifier 在工作预算耗尽后保留一次只收尾请求，不再开放工作工具。Code 提交已有成果，Research 根据已获得的来源摘录综合结果，Verifier 仅提交验收报告，Supervisor 仅完成或停止。最终请求关闭输出重生成和 SDK 重试，使用规则压缩避免额外摘要调用；失败后以明确的部分成果或未完成状态返回。

执行停止原因、交付状态、审核意见和 Supervisor 验收决定分别记录：`ready` 只是待验收声明；`not_performed` 表示审核未完成，不应解释成产物错误。正式 FINISH 要求最新 Result/Handoff/Verification 对应的 Supervisor 接受决定。失败处理按当前 Todo 和失败作用域决定，不能因为另一项 Code 任务停止就封锁所有 Code 任务。

相同只读参数反复取得相同内容会触发无进展保护；文件版本变化后允许重新取证。成功的同参数测试在文件版本未变化时不重复执行；明确的权限拒绝不会被普通读取或恢复清除。Verifier 只复用自己取得、文件版本未变化的证据，不能把 CodeAgent 自述当作独立验收。版本使用路径、文件大小与修改时间近似计算，不是操作系统隔离或强内容一致性保证。

收尾消费凭据保存在 `checkpoints/finalizations/`；任务模型与联网消费凭据保存在 `budgets/`，发送请求前先落盘。它们防止同一任务恢复后刷新额度，不是 Workflow 节点快照；审批恢复仍读取原有权威 Checkpoint，Trace 不参与恢复决策。

### 阶段预算与无进展控制

默认应用窗口为 131,072 token，额外保留 2,000 token 安全余量。输入估算包含消息、保留的思考字段和工具定义；工作与收尾使用不同输出额度，Context Monitor 与模型请求使用相同配置。输出上限是最大预留，不是每次必然消耗量。

| Agent | 工作轮数 / 工具上限 | 工作输出 / 收尾输出 token |
|---|---|---|
| Supervisor | 任务内 20 轮 / 32 次编排调用 | 16,384 / 16,384 |
| ResearchAgent | 每次 16 轮 / 8 次搜索 + 10 次提取 | 16,384 / 16,384 |
| CodeAgent | 每次 16 轮 / 32 次工具 | 32,768 / 16,384 |
| Verifier（条件调用） | 每次 6 轮 / 10 次证据工具 | 16,384 / 16,384 |

任务默认最多 5 次委派、64 次 Code 工具、40 次联网请求、128 次模型请求（包含路由、压缩和实际重试），其中 16 次模型请求保留给收尾。每个执行身份仍只允许一次收尾，不因尚有总额度就反复总结。同一 Todo 最多尝试两次，重新委派必须明确缺失证据、策略变化和预期证据。恢复旧任务不会刷新已冻结额度。

Base/Local 软压缩阈值为 32,000/48,000 token，Local 优先保留最近 8 组完整交互，并按 16,000 token 近期预算缩小组数；至少保留一组，不能拆开 ToolCall/ToolResult。完整输入达到可用预算 85% 时提前尝试压缩。任务、系统规则、TaskBoard 和受保护 Handoff 不交给摘要器改写。

Research 收尾只生成有界结论与来源 ID，再由程序补入真实 Observation 中的 URL 和标题，不复制整篇网页。结果整理失败与工具预算耗尽分别诊断；不能因整理失败再次搜索。已经完成整理、具有证据的部分研究成果仍进入 Gate，Supervisor 根据基础检查或条件独立审核报告判断是否接受或带限制交付。

全部预算可通过 `.env.example` 中的 `TIKI_OUTPUT_*`、`TIKI_BUDGET_*`、`TIKI_CONTEXT_*`、`TIKI_THINKING_*` 配置。更换模型时应同时检查真实窗口、输出能力和成本，而不是只提高循环次数。详见 [执行预算与进展策略](docs/execution-policy.md)。

真实 API Demo 会产生费用和外部请求，不属于默认测试套件。

## Documentation

| 文档 | 内容 |
|---|---|
| [Architecture](docs/architecture.md) | Control、Context、Execution、Application 四个平面 |
| [Design Decisions](docs/design-decisions.md) | 身份链、上下文隔离、恢复语义与关键不变量 |
| [Context & Failure Boundaries](docs/context-failure-boundaries.md) | 有界模型视图、失败 Turn 与恢复边界 |
| [Architecture Evolution](docs/architecture-evolution.md) | ToolCall → ReAct → LangGraph → Multi-Agent 演进 |
| [Textual TUI](docs/tui.md) | 交互界面、快捷键、Approval 与 Recovery |
| [Demo Validation](docs/demos.md) | 三类场景、产物、暂停和恢复 |
| [Changelog](CHANGELOG.md) | 发布变化与已知限制 |

## Current Limitations

- 单机 JSONL Checkpoint/History，不提供多进程文件锁或分布式 exactly-once；
- Token 使用字符近似估算，尚未接入供应商精确 Tokenizer；
- 正式应用使用有界 LLM Structured Summary，离线 Runtime 保留规则压缩器；摘要失败或硬约束仍超预算时明确停止，不无限扩大窗口；
- Workspace 是应用层路径与能力边界，不是操作系统 Sandbox；递归读取逐项校验路径，但不能消除恶意并发替换文件的竞态；
- Artifact-aware Verifier 首版聚焦 Python、HTML 和普通文本；
- Research Verification 能证明来源来自真实 Observation，不能自动证明网页内容绝对真实；
- Demo Validation 不提供任务集、重复采样、成本统计或 Single/Multi-Agent 量化对比。

## Contributing

欢迎通过 Issue 提交缺陷、设计讨论或新 Specialist Agent 建议。提交 PR 前请运行离线测试，并确保不提交 `.env`、`.tiki/`、`.tiki-demo/` 或真实 API Key。

## License

TikiAgent 使用 [MIT License](LICENSE) 发布。
