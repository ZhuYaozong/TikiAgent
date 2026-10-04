# 验证契约与执行边界

## 正式链路

Supervisor 通过 `update_plan` 创建 Todo，包含 `required_capabilities`、`acceptance_criteria`、`delivery_mode` 和依赖。每条验收有固定 `criterion_id`；已存在标准不能删除或放宽。能力检查是声明契约校验，不声称能确定性理解自然语言任务。

ResearchAgent 只有 Tavily Web 能力。CodeAgent 处理文件、命令和 Python 环境。`environment` 不暴露文件写工具，不强制交付报告；版本信息来自 `importlib.metadata`，不是不保证存在的包 `__version__`。

Specialist 完成后 Graph 构造独立 Verifier Base Context：原始任务、全局标准、当前 Todo/Handoff、当前 Result 和相关历史。不会继承 Specialist 内部 messages。Verifier 的短期取证消息经过同一个 ContextRuntime，预算与压缩仍生效。

Verifier 通过 `read_evidence` 查看结果附带证据，或通过文件、环境、测试工具独立取证；再单独调用 `submit_verification`。它不能写文件、安装包、调用任意命令、委派或结束任务。默认上限为 6 次模型调用、8 次工具调用。

## 通过条件

- Gate 验证已完成 Handoff 与当前 Result 身份。
- 报告必须对应当前 Todo，所有验收项恰好出现一次。
- 必需项必须 passed；passed 必须引用存在且可用的真实证据，不能只引用 Result 声明。
- 命令非零退出、超时、验证工具明确 verified=false 不能作为成功证据。
- Research 来源仍必须追溯到真实搜索 Observation；有来源不等于语义验收通过。
- Code 未完成或触发运行预算停止，不允许模型宣称 PASS。
- FINISH 再次检查 TaskBoard 中每个 Todo 的最新身份、报告和标准。

语义相关性仍由模型判断，以上规则不保证事实百分之百正确，也不替代统计评测。旧规则 Verifier 保留给基线和测试；正式应用不再使用固定的“有 Python 文件就必须 unittest”策略。

## 工具与隔离

`inspect_python_environment(package)` 使用运行 TikiAgent 的固定解释器，读取包元数据，不导入目标包。`probe_python_import(module)` 在 `-I -B` 子进程中检查已安装模块导入；不用于 Workspace 内本地模块。模型不能提供解释器路径或 Python 源码。

`run_verification_tests(runner)` 只接受 unittest/pytest 枚举，复制至临时目录后运行，跳过符号链接/junction、虚拟环境和 `.env`，限制 1000 文件/10MB、30 秒和返回文本大小。无测试不算通过，pytest 禁用自动插件加载。验证子进程不继承 API Key 等非必要环境变量。

这些措施提供能力隔离和有限运行隔离，**不是 OS Sandbox**。导入和测试依然执行代码，可信包也可能有副作用；测试可自行访问绝对路径或网络。请只运行可信 Workspace 和依赖。子进程输出在返回时截断，尚不是流式硬内存限制。

安装包继续经 run_command → Permission ASK → Checkpoint → Approval → 执行 → Resume，不由 Verifier 负责。CodeAgent 普通查询无需审批，但仍经过 Coordinator 保存 executing/completed 工具级 Checkpoint。Verifier 取证使用独立 Harness，不增加自身的恢复快照；此次不提供 Verifier 中途恢复或 exactly-once 保证。

## 失败、记录与兼容性

报告携带 `failure_category/retryable/blocking_reason`；明确不可重试时禁止再次委派同一失败 Todo。Supervisor 应报告权限、能力或预算阻塞，而不是创建脚本绕过限制。可修复的验收失败仍可重新规划，但不能削弱验收标准。

Gate/报告进入 History；只保留被引用的有界证据摘要，不复制完整执行输出。工具调用与结果由统一 EventBus 转发到 Trace，Verifier 使用同样的事件语义；Trace 不参与恢复。模型空响应最多原请求重试一次，不重放工具；诊断只含响应类型、finish_reason、是否有文本/工具以及空响应重试数。

旧 TaskBoard/Handoff JSON 缺少新增字段时可以读取。但进入正式 Agent 验证若没有冻结契约，会保守拒绝通过，需要重新发起任务；不会从 Trace 猜测或自动补造验收标准。此次不新增 Workflow 节点级 Checkpoint。
