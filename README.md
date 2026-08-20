# TikiAgent

TikiAgent 是一个渐进式构建的 **Multi-Agent Task Execution System**。目标是根据任务动态调度不同的 Specialist Agent，并通过验证闭环、上下文工程和安全执行环境完成 Research、Coding 与复合任务。

当前版本为 **v0.1 ReAct Foundation**：已实现真实模型驱动的 Agent Loop、OpenAI-compatible 模型适配层，以及带 Workspace 边界的文件 Execution Harness。

## v0.1 架构

```text
User Task
    ↓
ReAct Agent Loop
    ↓
ModelClient ── OpenAI-compatible ── DeepSeek / vLLM
    ↓ ToolCall
Dispatcher
    ↓
Tool Registry ── File Tools
    ↓
Workspace Boundary
    ↓ ToolResult / Observation
ReAct Agent Loop
    ├── Continue
    └── Final Answer
```

当前职责边界：

- **ModelClient**：把内部 Tool Schema 和模型响应适配为 OpenAI-compatible Chat Completions 协议；
- **ReActAgent**：保存消息、关联 `tool_call_id`、执行循环并控制 `max_steps`；
- **Dispatcher**：验证工具名称和参数、执行 Python 工具、返回结构化错误；
- **Workspace**：拒绝绝对路径和目录逃逸；
- **File Tools**：提供 `read_file`、`write_file`、`edit_file`、`list_files` 和 `grep`。

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

运行真实模型文件修复 Demo：

```powershell
uv run python examples/react_file_repair.py
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

## 项目结构

```text
src/tikiagent/
├── agents/
│   └── react.py
├── harness/
│   ├── dispatcher.py
│   ├── file_tools.py
│   ├── models.py
│   ├── registry.py
│   └── workspace.py
└── llm/
    ├── config.py
    ├── models.py
    └── openai_compatible.py
```

测试不调用外部模型 API，使用确定性 Fake Model 验证 Agent Loop，并覆盖参数错误、未知工具、路径逃逸、编辑歧义、非法 JSON 和 `max_steps`。

## 当前限制

- 文件复查只能证明文本状态，尚未通过 Bash/Test Tool 验证程序行为；
- 尚未实现 Permission、Human Approval、Checkpoint 和 Trace；
- 当前只有 Single ReAct Agent，尚未引入 LangGraph、Supervisor 和 Specialist Agents；
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

最终系统分为 Control Plane、Context Plane 和 Execution Plane。v0.1 完成的是 Execution Plane 的文件工具基础，以及后续 Specialist Agent 可以复用的 ReAct 执行核心。

## Roadmap

- [x] 初始化 Python 项目和测试；
- [x] OpenAI-compatible `ModelClient`；
- [x] Tool Registry、Dispatcher 与结构化 ToolResult；
- [x] Workspace Boundary 与文件工具；
- [x] 真实模型 ReAct Agent 和 `max_steps`；
- [ ] Bash/Test Tool 与完整 Execution Harness；
- [ ] LangGraph 与结构化 TikiState；
- [ ] Plan → Execute → Verify；
- [ ] Supervisor、ResearchAgent 与 CodeAgent；
- [ ] History、Retriever、Context Builder 与 Compressor；
- [ ] Permission、Approval、Checkpoint、Trace；
- [ ] Session、CLI、Event Stream 与 Evaluation。

## v1 目标 Demo

1. Research：搜索重要 AI Agent 新闻并总结来源；
2. Coding：创建 Python 项目并运行测试；
3. Multi-Agent：调研 Agent Framework 的变化，并根据调研结果生成对比网页。
