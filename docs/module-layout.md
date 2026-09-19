# 模块导航

TikiAgent 按运行职责组织源码，而不是按开发阶段累积模块。新增能力时优先放进已有职责目录；只有独立的生命周期或依赖边界才值得新增子包。

## 从哪里读起

| 要了解的行为 | 源码入口（相对于 `src/tikiagent/`） |
|---|---|
| 用户输入如何启动任务 | `interfaces/cli.py`、`interfaces/tui/app.py` |
| 应用如何装配、保存会话和调用工作流 | `application/bootstrap.py`、`controller.py`、`workflow_adapter.py` |
| 谁决定委派、重试和结束 | `agents/supervisor.py` |
| 多 Agent 如何流转 | `orchestration/workflow.py`、`state.py`、`contracts.py` |
| Research / Code 如何完成一次委派 | `agents/research.py`、`agents/code.py` |
| 单 Agent 如何调用工具、暂停和恢复 | `runtime/react.py`、`resumable.py`、`models.py` |
| 最新 Result 是否已通过验证 | `verification/gate.py`、`orchestration/guards.py` |
| 模型输入如何构建和控制预算 | `context/builder.py`、`preparation.py`、`call_context.py` |
| 工具如何校验参数并调用实现 | `tools/registry.py`、`dispatcher.py` |
| 工具是否允许执行、如何恢复 | `harness/execution.py`、`coordinator.py` |
| 模型和搜索服务如何接入 | `providers/llm/`、`providers/search/` |

## 目录与内部分类

```text
src/tikiagent/
├── agents/                 # 角色策略，而非通用循环或验证工具箱
│   ├── supervisor.py
│   ├── research.py
│   └── code.py
├── runtime/                # 单 Agent 执行生命周期
│   ├── react.py
│   ├── resumable.py
│   └── models.py
├── orchestration/          # 多 Agent 协作与控制流
│   ├── workflow.py
│   ├── state.py
│   ├── contracts.py         # Decision、Handoff、Result 等协作协议
│   ├── guards.py            # 最新结果身份与待办检查
│   └── completion.py        # 面向用户的最终答案整理
├── verification/           # 统一 Gate 与不同证据的验证实现
│   ├── gate.py
│   ├── environment.py
│   ├── research.py
│   ├── artifacts.py
│   └── reports.py
├── context/
│   ├── builder.py          # 检索并组装 Base Context
│   ├── profiles.py         # 角色信息需求
│   ├── preparation.py      # 协调输入准备、监控和压缩
│   ├── prompt.py
│   ├── call_context.py
│   ├── tool_selection.py
│   ├── task_board.py
│   ├── finalization.py
│   ├── models.py           # Context、任务视图与调用结构
│   ├── schema.py           # 共享校验基类
│   ├── memory/             # history、retriever、notepad、local、models
│   └── compression/        # monitor、compressors、policy、models
├── tools/                  # models、registry、dispatcher、files、commands、web
├── harness/
│   ├── execution.py        # Exposure → Prepare → Permission / Approval → Execute
│   ├── coordinator.py      # 执行生命周期与持久化协调
│   ├── exposure.py
│   ├── scope.py            # 执行作用域，避免审批模型循环依赖
│   ├── workspace.py
│   ├── models.py
│   ├── permissions/        # policy、approval、models
│   └── persistence/        # checkpoint、recovery、trace
├── providers/
│   ├── llm/                # OpenAI 兼容模型客户端、配置与结构化输出
│   └── search/             # Tavily 配置与 HTTP 适配
├── application/            # bootstrap、controller、workflow_adapter、session 等
├── interfaces/
│   ├── cli.py
│   └── tui/                # app、adapter、presenter、modals、styles.tcss 等
├── baselines/              # react_graph、plan_verify、planner、code_actor、fixed_file_verifier
└── demo/                   # 三类演示场景与交付结果收集
```

## 职责和依赖边界

- **Interfaces → Application**：CLI/TUI 通过 Controller 发起操作；TUI 的 Adapter/ViewState 只做显示投影，不保存第二套审批事实。
- **Application → Orchestration**：Bootstrap 是装配入口；Workflow Adapter 转发内部事件，Controller 不反推工具执行状态。
- **Orchestration → Agents / Verification**：工作流组织路由，Supervisor 做决策，Verifier 报告证据。成功必须对应最新 `result_id/handoff_id`。
- **Agents → Runtime / Context / Harness**：角色使用通用循环与上下文能力，不把另一角色的内部 Messages 当成自己的历史。
- **Runtime → Harness → Tools**：正式执行路径经过 Harness。Dispatcher 只处理校验与调用，直接 `dispatch()` 仍是 legacy/internal API，不代表获得执行权限。
- **Tools → Providers**：Web Tool 定义模型可调用的参数和结果；Tavily 的 HTTP 行为属于 Provider。Workspace 与 Runtime Enforcement 仍在实际执行时生效。
- **Context 内部**：`memory/` 是可检索来源和局部交互；`compression/` 控制预算；`preparation.py` 协调它们，不把 Notepad 当作摘要。
- **Harness 内部**：`permissions/` 判断授权；`persistence/` 保存 Checkpoint 和审计 Trace。Checkpoint 是恢复权威，Trace 不参与恢复决策。
- **Baselines**：仅供独立示例、对照和兼容导出使用。正式 Bootstrap 和 TUI 启动不加载 Baseline；测试对此做新进程断言。

这些是职责和调用方向，不是完全无环的静态包依赖声明。协议模块仍可能被多个层共同使用；本次没有引入额外 DI 框架，也没有为了消除所有共享类型而新增通用大包。

## 导入路径迁移

命令名仍为 `tikiagent`、`tikiagent-tui`、`tikiagent-demo`。`.env` 配置方式及 Checkpoint、State、Approval、ToolResult、History 的 JSON 协议不变。源码路径变化后请执行 `uv sync --locked` 更新本地入口。

直接导入旧子模块的 Python 代码需要更新。下表路径均省略 `tikiagent.` 前缀：

| 旧位置 | 新位置 |
|---|---|
| `agents.react`、`agents.resumable` | `runtime.react`、`runtime.resumable`；运行结果类型在 `runtime.models` |
| `agents.planner` | `baselines.planner` |
| `agents.code.ReActCodeActor` | `baselines.code_actor.ReActCodeActor` |
| `agents.verifier` | `verification.environment/research/artifacts/reports`；固定文件基线在 `baselines.fixed_file_verifier` |
| `agents.supervisor` 中的结果检查函数 | `orchestration.guards` |
| `orchestration.multi_agent`、`orchestration.models` | `orchestration.workflow`、`orchestration.contracts` |
| `orchestration.verification_gate` | `verification.gate` |
| `orchestration.react_graph`、`orchestration.plan_verify` | `baselines.react_graph`、`baselines.plan_verify` |
| `harness.registry/dispatcher` | `tools.registry/dispatcher` |
| `harness.file_tools/command_tools` | `tools.files/commands` |
| `harness.web_tools` | 工具在 `tools.web`；服务配置/适配在 `providers.search` |
| `harness.models` | 工具协议在 `tools.models`，审批协议在 `harness.permissions.models`，作用域在 `harness.scope`，执行结果仍在 `harness.models` |
| `harness.guards` | `harness.exposure` |
| `harness.permission/approval` | `harness.permissions.policy/approval` |
| `harness.checkpoint/recovery/trace` | `harness.persistence.checkpoint/recovery/trace` |
| `context.history/retriever/notepad/local_memory` | `context.memory.history/retriever/notepad/local` |
| `context.monitor/compressor` | `context.compression.monitor/compressors` |
| `context.runtime` | `context.preparation`；压缩策略在 `context.compression.policy` |
| `context.tool_view` | `context.tool_selection` |
| `context.models` | Context 类型仍保留；历史/局部记忆类型在 `context.memory.models`，预算类型在 `context.compression.models` |
| `llm.*` | `providers.llm.*` |
| `application.runtime/workflow` | `application.bootstrap/workflow_adapter` |
| `application.cli`、`tui.*` | `interfaces.cli`、`interfaces.tui.*` |

例如：

```python
from tikiagent.runtime.react import ReActAgent
from tikiagent.tools.models import ToolCall, ToolResult
from tikiagent.harness.execution import ExecutionHarness
from tikiagent.providers.llm.openai_compatible import OpenAICompatibleClient
```

`agents`、`orchestration`、`harness`、`context`、`application` 保留已有包级公开名称的延迟兼容导出，例如 `from tikiagent.agents import ReActAgent`。这不等于保留所有旧子模块，也不包含已经迁走的 `tikiagent.llm` 和 `tikiagent.tui` 包。新代码应直接导入上表中的职责模块。

本次不迁移 `.tiki*` 运行数据、不重置会话，也不变更 `TikiAgent_learn`。Git 提供源文件迁移记录；需要回退时应按提交回退代码，而不是删除用户 Workspace。
