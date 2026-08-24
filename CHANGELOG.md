# Changelog

本项目从 v0.1 开始保留可运行的架构演进基线。这里只记录面向使用者的发布变化。

## 1.0.0 - 2026-08-25

### Added

- Supervisor、ResearchAgent、CodeAgent 与统一 Verification Gate；
- Context Engine：History、Retriever、Task Board、Context Profiles、Context Builder、Monitor、Compressor 与 Notepad；
- Execution Harness：Tool Exposure、Permission、Approval、Workspace/Runtime Enforcement、Checkpoint/Resume 与 Trace；
- Application Plane：Session、Turn、Intent Router、Event Stream、CLI 与人工恢复入口；
- Conversation-first Textual TUI，支持多轮 Session、Approval、Recovery、只读 Workspace 和 Markdown 最终回答；
- Research、Coding 与 Hybrid 三类可重复 Demo Validation；
- 使用 MIT License 发布。

### Safety

- Result 与 Verification 通过 `result_id/handoff_id/subject_agent` 绑定；
- ASK 在 Checkpoint 成功持久化后暂停，不生成伪造 ToolResult；
- `executing` 崩溃后禁止自动重放，必须人工 Recovery/Reconcile；
- 最终回答使用独立长度预算，完整网页摘录保留为 History 证据，避免长结果阻塞 Finalization；
- Event、Trace 与 TUI 投影执行脱敏和输出截断，不作为恢复事实。

### Known limits

- Workspace Boundary 不是操作系统级 Sandbox；
- JSONL 持久化面向单机 v1，不提供分布式 exactly-once；
- Token 预算使用字符近似值；
- Demo Validation 不是统计 Evaluation，不声明未经实验支持的量化收益。
