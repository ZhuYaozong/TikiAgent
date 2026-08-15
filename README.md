# TikiAgent

TikiAgent 是一个渐进式构建的 **Multi-Agent Task Execution System**。它的目标不是只实现 Coding Agent，而是根据任务动态调度不同的 Specialist Agent，并通过验证闭环、上下文工程和安全执行环境完成复合任务。

## 目标架构

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

系统最终分成三个平面：

- **Control Plane**：Supervisor、Specialist Agents、Verifier Loop；
- **Context Plane**：State、History、Retriever、Context Builder、Monitor、Compressor；
- **Execution Plane**：Tools、Workspace、Permission、Approval、Checkpoint、Trace。

核心职责边界：

```text
Agent   = 决定做什么
Harness = 决定这件事如何执行
```

## 演进路线

```text
Tool Calling
    ↓
ReAct Agent
    ↓
Tool / Workspace / Harness
    ↓
LangGraph
    ↓
Plan → Execute → Verify
    ↓
Supervisor / Multi-Agent
    ↓
Context Engineering
    ↓
Harness Engineering
    ↓
Session / Application
    ↓
Evaluation
```

## 当前进度

### Day 1：工程基线与 Tool Calling 学习

- 正式仓库完成最小 Python `src` 项目结构；
- Tool Calling、Dispatcher 与最小 Agent Loop 在本地 `TikiAgent_learn` 项目中进行教学实验；
- 正式 Harness 将在 Day 2 基于已验证的职责边界重新实现。

Day 1 刻意不把教学实现复制进正式项目，避免把示例代码误当成正式执行层设计。

## 本地开发

环境要求：

- Python 3.13+
- uv

安装并运行测试：

```powershell
uv sync
uv run pytest
```

如果 Windows 环境配置了不可用的 Python 镜像或证书目录，可以仅对当前命令改用系统证书与官方索引：

```powershell
uv sync --native-tls --default-index https://pypi.org/simple
uv run --native-tls --default-index https://pypi.org/simple --locked pytest
```

Day 1 验证结果：正式项目骨架测试通过；Tool Calling 的 9 项故障与循环测试在本地学习项目中通过，三个渐进示例均可运行。

## v1 目标 Demo

1. Research：搜索重要 AI Agent 新闻并总结来源；
2. Coding：创建 Python 项目并运行测试；
3. Multi-Agent：调研 Agent Framework 的变化，并根据调研结果生成对比网页。
