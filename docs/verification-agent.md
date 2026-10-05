# 验证契约与执行边界

## 正式链路

Supervisor 通过 `update_plan` 创建 Todo，包含 `required_capabilities`、`acceptance_criteria`、`delivery_mode` 和依赖。每条验收有固定 `criterion_id`；已存在标准不能删除或放宽。能力检查是声明契约校验，不声称能确定性理解自然语言任务。

ResearchAgent 只有 Tavily Web 能力。CodeAgent 处理文件、命令和 Python 环境。`environment` 不暴露文件写工具，不强制交付报告；版本信息来自 `importlib.metadata`，不是不保证存在的包 `__version__`。

Specialist 完成后 Graph 构造独立 Verifier Base Context：原始任务、全局标准、当前 Todo/Handoff、当前 Result 和相关历史。不会继承 Specialist 内部 messages。Verifier 的短期取证消息经过同一个 ContextRuntime，预算与压缩仍生效。

Verifier 通过 `read_evidence` 查看结果附带证据，或通过文件、环境、测试工具独立取证；再单独调用 `submit_verification`。它不能写文件、安装包、调用任意命令、委派或结束任务。

正式应用取证阶段默认最多 4 轮模型调用、6 次取证工具尝试（含被 Harness 拒绝的尝试）。提交报告不占取证次数。任一取证预算耗尽且尚未提交时，先保存本轮全部 ToolCall/ToolResult，再增加唯一一次收尾模型调用；收尾仅暴露并允许 `submit_verification`，不能继续取证。因此默认审核循环至多调用模型 5 次，不含适配器自身有限的空响应/网络重试及 ContextRuntime 的摘要调用。

每轮上下文显示剩余取证次数、轮数和已读证据索引，同一证据可支持多条验收条件。批量调用超出剩余预算时，未执行项返回 `verification_evidence_budget_exceeded`，不调用 handler，仍保持消息完整配对。收尾必须单独提交；普通文本、继续取证或非法报告都不会再获得一次机会，而是返回 `model_response / retryable=false`。这表示验证未完成，不代表产物已经被判定不合格；合法的证据不足报告仍归类为 `insufficient_evidence`，绝不自动 PASS。

## 审核与验收分离

`VerificationReport.passed` 表示“报告认为必需条件均已满足”，不是整个任务的成功/失败裁决。正式流程为：

```text
Specialist Result → Verifier 报告 → Todo.awaiting_review
                                      ↓
                              Supervisor.review_result
                 accept / accept_with_limitations / request_changes / stop
                   ↓                  ↓                   ↓          ↓
               completed          completed             failed     stopped
```

验收决定必须绑定当前 Todo 最新 `result_id/handoff_id/verification_id`，包含理由。`accept_with_limitations` 必须填写限制；程序另保留报告中的未满足项或审核未完成原因，不修改原始标准或报告。接受决定保存为 Todo.review 和不可变 History 记录，随 Workflow Snapshot 恢复并传给下游依赖，最终回答先披露限制再展示交付。未完成审核也可以对已有真实成果明确带限制接受，不能声称已经通过审核。

`request_changes` 不自动重跑：再次委派仍受失败作用域、权限及预算保护，必须说明缺失证据、策略变化和预期新证据。`stop` 则停止工作流。不完整但有用的质量结果不必被 Verifier 强制阻塞后续工作。

## 不可覆盖的机械边界

- Gate 验证已完成 Handoff 与当前 Result 身份。
- 报告必须对应当前 Todo，所有验收项恰好出现一次。
- 单项 passed 必须引用存在且可用的真实证据，不能只引用 Result 声明。未满足必需项可以明确带限制接受，但不能删除条件或宣称完整通过。
- 命令非零退出、超时、验证工具明确 verified=false 不能作为成功证据。
- Research 来源仍必须追溯到真实搜索 Observation；有来源不等于语义验收通过。
- 没有实际交付、未形成交付的权限阻塞或硬性身份/证据错误不能接受；正式 artifact 模式还检查交付路径在 Workspace 内且文件实际存在。
- FINISH 再次检查每个 Todo 的最新身份、报告、Supervisor 接受决定及限制，不再要求报告全部 PASS。

语义相关性仍由模型判断，以上规则不保证事实百分之百正确，也不替代统计评测。旧规则 Verifier 保留给基线和测试；正式应用不再使用固定的“有 Python 文件就必须 unittest”策略。

## 工具与隔离

`inspect_python_environment(package)` 使用运行 TikiAgent 的固定解释器，读取包元数据，不导入目标包。`probe_python_import(module)` 在 `-I -B` 子进程中检查已安装模块导入；不用于 Workspace 内本地模块。模型不能提供解释器路径或 Python 源码。

`run_verification_tests(runner)` 只接受 unittest/pytest 枚举，复制至临时目录后运行，跳过符号链接/junction、虚拟环境和 `.env`，限制 1000 文件/10MB、30 秒和返回文本大小。无测试不算通过，pytest 禁用自动插件加载。验证子进程不继承 API Key 等非必要环境变量。

这些措施提供能力隔离和有限运行隔离，**不是 OS Sandbox**。导入和测试依然执行代码，可信包也可能有副作用；测试可自行访问绝对路径或网络。请只运行可信 Workspace 和依赖。子进程输出在返回时截断，尚不是流式硬内存限制。

安装包继续经 run_command → Permission ASK → Checkpoint → Approval → 执行 → Resume，不由 Verifier 负责。CodeAgent 普通查询无需审批，但仍经过 Coordinator 保存 executing/completed 工具级 Checkpoint。Verifier 取证使用独立 Harness，不增加自身的恢复快照；此次不提供 Verifier 中途恢复或 exactly-once 保证。

## 失败、记录与兼容性

报告携带 `failure_category/retryable/blocking_reason`；不可重试提示约束再次执行，不替代 Supervisor 的质量取舍。Supervisor 应报告权限、能力或预算阻塞，而不是创建脚本绕过限制。可修复的验收缺口仍可要求补做并重新规划，也可对已有交付明确带限制接受，但不能削弱验收标准或伪造执行事实。

Gate/报告进入 History；只保留被引用的有界证据摘要，不复制完整执行输出。工具调用与结果由统一 EventBus 转发到 Trace，Verifier 使用同样的事件语义；Trace 不参与恢复。模型空响应最多原请求重试一次，不重放工具；诊断只含响应类型、finish_reason、是否有文本/工具以及空响应重试数。

旧 TaskBoard/Handoff JSON 缺少新增字段时可以读取。旧正式快照只有 completed/PASS 而没有 Supervisor review 时，恢复为 awaiting_review，不能自动补造接受决定。进入正式 Agent 验证若没有冻结契约，会保守拒绝，需要重新发起任务；不会从 Trace 猜测或自动补造验收标准。旧结构化基线继续使用 PASS 门控；未新增 Workflow 节点级 Checkpoint。
