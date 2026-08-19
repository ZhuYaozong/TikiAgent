# TikiAgent

TikiAgent 是一个渐进式构建的 **Multi-Agent Task Execution System**。它的目标不是只实现 Coding Agent，而是根据任务动态调度不同的 Specialist Agent，并通过验证闭环、上下文工程和安全执行环境完成复合任务。

> 项目目前处于早期开发阶段，仅完成基础工程骨架。公开 API、模块边界和运行方式仍可能变化。

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

## 核心设计目标

- 根据任务动态规划、路由和委派 Specialist Agent；
- 支持 Research、Coding 以及两者协作的复合任务；
- 使用 Verifier 提供基于环境证据的执行闭环；
- 隔离不同 Agent 的工作上下文，避免传递完整历史；
- 通过 Execution Harness 统一处理工具注册、参数验证和安全边界；
- 支持 Permission、Human Approval、Checkpoint、Resume 与 Trace；
- 使用可复现评测比较 Single-Agent 与 Multi-Agent 的收益和成本。

## Roadmap

- [x] 初始化 Python 项目骨架与基础测试；
- [ ] 实现 Tool Registry、Dispatcher 与 Workspace Boundary；
- [ ] 实现最小 ReAct Agent 与 Execution Harness；
- [ ] 引入 LangGraph 和结构化 Workflow State；
- [ ] 实现 Plan → Execute → Verify 闭环；
- [ ] 实现 Supervisor、ResearchAgent 与 CodeAgent；
- [ ] 实现 History、Retriever 与 Context Builder；
- [ ] 实现 Context Monitor、Compressor 与 Notepad；
- [ ] 实现 Permission、Approval、Checkpoint 与 Trace；
- [ ] 实现 Session、CLI、Event Stream 与 Evaluation；
- [ ] 完成 Research、Coding 和 Multi-Agent 演示。

## 项目结构

```text
TikiAgent/
├── src/
│   └── tikiagent/
├── tests/
├── pyproject.toml
└── README.md
```

项目将随着 Roadmap 推进逐步增加 `agents`、`harness`、`orchestration` 和 `context` 等模块。

## Quick Start

环境要求：

- Python 3.13+
- uv

克隆仓库并运行测试：

```powershell
git clone https://github.com/ZhuYaozong/TikiAgent.git
cd TikiAgent
uv sync
uv run pytest
```

## v1 目标 Demo

1. Research：搜索重要 AI Agent 新闻并总结来源；
2. Coding：创建 Python 项目并运行测试；
3. Multi-Agent：调研 Agent Framework 的变化，并根据调研结果生成对比网页。
