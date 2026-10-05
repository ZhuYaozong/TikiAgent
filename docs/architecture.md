# TikiAgent v1.0 Architecture

TikiAgent 将 Multi-Agent 系统拆成 Control、Context、Execution 和 Application 四个平面。每个平面拥有独立职责，避免把模型决策、运行时安全、上下文管理和产品会话混在同一个 Agent Loop 中。

本文说明系统职责；对应的源码入口和目录边界见 [模块导航](module-layout.md)。

## 总览

```text
User / CLI / TUI
        │
        ▼
Application Plane
Session · Turn · Intent Router · Event Stream
        │
        ▼
Control Plane
Supervisor ──delegate──▶ ResearchAgent / CodeAgent
    ▲                           │
    └──── Verification Gate ◀───┘
        │
        ├──────── Context Plane
        │         State · Task Board · History · Notepad
        │         Retriever · Context Builder · Monitor · Compressor
        │
        └──────── Execution Plane
                  Exposure · Permission · Approval
                  Workspace · Runtime · Checkpoint · Trace
```

## Control Plane

Supervisor 负责理解、规划、委派、重试和结束判断。Specialist 只完成自己的任务：ResearchAgent 进行检索与来源整理，CodeAgent 操作 Workspace 并运行验证。每个 Specialist Result 都进入 Verification Gate，Verifier 只报告证据和失败原因，最终路由仍由 Supervisor 决定。

正式应用的 Gate 内部使用独立 `VerifierAgent` 按冻结的 Todo 验收条件取证。Gate 和 FINISH 保留身份、覆盖与证据引用的机械检查；旧规则验证器只作为基线/测试实现。环境任务与产物任务分别验收，不按文件扩展名强加测试。详见 [验证契约](verification-agent.md)。

结束条件不是“某个工具成功返回”，而是：

```text
最新 Specialist Result
        +
匹配 result_id / handoff_id / subject_agent 的审核报告
        +
匹配最新 todo_id / result_id / handoff_id / verification_id 的 Supervisor 接受决定
        +
全部必要 Todo 已完成
        ↓
Supervisor FINISH
```

正式工具型 Supervisor 使用 `review_result` 将“审核意见”与“验收决定”分离。Verifier 不修改 Todo 完成状态；审核后 Todo 等待 `awaiting_review`。接受/带限制接受才会完成该 Todo 并解锁依赖，补做/停止则保留失败事实。质量缺口可以明确接受，身份错配、非法证据、权限阻塞与不存在的文件不可覆盖。旧结构化基线仍使用原 PASS 门控。

## Context Plane

正式 Supervisor 现在具备自己的编排工具循环和 LocalMemory。计划、委派及两层 LLM 摘要的具体边界见 [Supervisor 与上下文](supervisor-context.md)。Graph 负责执行和验证，FINISH 逐 Todo 校验准确的结果身份。

`TikiState` 保存当前 Workflow 的结构化运行事实，不保存完整历史。完整可复用信息进入 History，长期关键事实进入 Notepad，Task Board 独立跟踪多个 Todo。

```text
State / Task Board / History / Notepad
                    ↓
                 Retriever
                    ↓
              Context Builder
                    ↓
                Base Context
                    +
Prompt / Tool Schemas / Local ReAct Messages
                    ↓
             Context Monitor
                    ↓
          必要时分别压缩 Base / Local
```

切换 Agent 或重新委派时重新构建 Base Context，不继承上一位 Specialist 的内部 messages。单次 `Agent.run()` 内部保留 short-term ReAct interactions，并保证 ToolCall 与对应 ToolResult 原子配对。

## Execution Plane

正式工具执行必须经过 Execution Harness：

```text
ToolCall
  ↓ basic validation
Tool Exposure Guard
  ↓ argument validation / canonicalize
  ↓
Permission: ALLOW / ASK / DENY
  ↓ ASK 时绑定 task / session / workspace / fingerprint
Approval
  ↓
Checkpoint(executing)
  ↓
Workspace / Runtime Enforcement
  ↓
ToolResult + Trace
```

Harness 的五个核心职责是：

- Gate：判断当前调用能否进入执行管线；
- Enforce：未通过规则或审批就不执行 handler；
- Isolate：限制 Agent 能力集合和 Workspace/Runtime 边界；
- Persist：使用权威 Checkpoint 支持暂停与恢复；
- Observe：使用 Event 和 Trace 记录实际发生的事情。

Checkpoint 是恢复事实的唯一权威来源；Trace 只用于审计。处于 `executing` 的副作用调用在崩溃后进入 `recovery_required`，系统不会猜测执行结果或自动重放。

## Application Plane

```text
Session = 多轮对话边界
Turn    = 一次用户输入与系统响应
Task    = 一次需要完成的业务目标
Run     = Workflow 的一次实际执行尝试
```

Session 只保存身份、Turn 计数和 Checkpoint 引用，不内嵌完整 History。Intent Router 只区分 CHAT 与 WORKFLOW；Supervisor 只在 WORKFLOW 内负责语义规划。Application Event 经统一 EventBus 脱敏、截断并投影到 CLI/TUI，不能反向成为 Workflow 或恢复事实。

## 关键边界

```text
Agent 决定做什么          Harness 决定怎样执行
Retriever 决定找什么      Context Builder 决定怎样组装
History 保存可复用信息    Trace 保存执行轨迹
Memory 支持当前工作       Checkpoint 支持崩溃恢复
Tool Selection 控制展示   Permission 控制是否允许执行
Verification 提供判断证据 Supervisor 决定下一条控制边
```

## 入口

- [README Quick Start](../README.md#quick-start)
- [Design Decisions](design-decisions.md)
- [Demo Validation](demos.md)
- [Conversation-first TUI](tui.md)
- [Architecture Evolution](architecture-evolution.md)
