# Architecture Evolution

TikiAgent 保留早期可运行示例，用来解释每个架构模块解决了什么问题。正式 v1.0 不在文件名中继续叠加 `final` 或版本后缀，而是在同一套语义模块上演进。

## v0.1 Tool Calling 与 ReAct

```text
Model ToolCall → Dispatcher → Python Tool → ToolResult → Model
```

Tool Calling 只是模型生成结构化调用请求；真正执行函数的是 Dispatcher 与本地 handler。ReAct Loop 加入 Observation、终止条件、工具错误反馈和 Workspace Boundary。

可运行基线：`examples/react_file_repair.py`。

## v0.2 LangGraph 与 State

手写循环被转换为 Actor、Tools 和 Conditional Edge。`TikiState` 保存结构化 Workflow 事实，messages 只是其中一个带 Reducer 的字段。

可运行基线：`examples/langgraph_react.py`。

## v0.3 Plan → Execute → Verify

Verifier 使用只读能力检查环境结果，无论 PASS/FAIL 都返回 Planner。Planner 决定重试或结束，避免固定 `Verifier FAIL → Actor` 失去重新规划能力。

可运行基线：`examples/plan_verify_repair.py`。

## v0.4 Multi-Agent

Planner/Actor 演进为 Supervisor、ResearchAgent、CodeAgent 和 Verification Gate。Agent 通过结构化 Handoff/Result 协作，不传递全部内部 messages；Result 与 Verification 使用身份链防止旧 PASS 验证新结果。

可运行基线：`examples/multi_agent_report.py`。

## v0.5 Context Engine

完整 History 移出 State，Retriever 和 Context Builder 分离；不同 Agent 使用不同 Context Profile。Context II 加入完整模型调用预算、Base/Local 两类压缩、动态 Prompt/Tool View、Notepad、Scope Transition 和 Finalization。

## v0.6 Execution Harness

基础 Registry、Dispatcher、Workspace、Timeout 与 Output Limit 演进为 Gate / Enforce / Isolate / Persist / Observe。Approval、Checkpoint、Resume、Recovery、Reconcile 和 Trace 形成可恢复执行闭环。

可运行示例：`examples/harness_security.py`、`examples/harness_resume.py`。

## v0.7～v1.0 Application

Session、Turn、Intent Router、Event Stream 和 CLI 将 Workflow 变成可持续使用的应用。Textual TUI 提供 Conversation-first 交互，Artifact-aware Verification 和三类 Demo Validation 补齐真实交付路径。v1.0 最终冻结 Research、Coding 与 Hybrid 三个主要场景。

## 为什么保留 Baseline

这些示例不是正式系统的重复实现，而是用于回答：

- ToolCall 为什么不等于 Agent；
- State、Memory、History、Checkpoint 和 Trace 为什么不能合并；
- Verifier 为什么不修复结果，也不决定路由；
- Multi-Agent 的 Context/Capability Isolation 如何实现；
- Harness 为什么不能只是一层裸 `subprocess.run()`。

项目当前不提供统计 Evaluation，因此不会根据这些 Baseline 宣称 Multi-Agent 在成功率、成本或延迟上必然优于 Single-Agent。
