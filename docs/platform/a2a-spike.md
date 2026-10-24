# B05 A2A 双端兼容性实测

日期：2026-10-24。基线：TiKi main `b72a24b80185c44a7cd041870c4a384aac8e2046`。

选择 A2A 1.0.0 + 官方 a2a-sdk 1.2.3 + JSON-RPC/SSE。两组独立客户端/服务端进程通过真实 localhost HTTP，**每组 56 项，共 112 项通过**。

| 组合 | 客户端 Python | 服务端 Python | SDK | 结果 |
|---|---|---|---|---|
| TiKi → JobPilot 目标方向 | 3.13.12 | 3.12.10 | 两端 1.2.3 | 56/56 |
| 反向兼容性检查 | 3.12.10 | 3.13.12 | 两端 1.2.3 | 56/56 |

完整版本和断言见 [脱敏证据](a2a-spike-results.json)，复现见 [独立工程](../../integration_tests/platform/a2a_spike/README.md)，冻结约定见 [binding v1](../contracts/a2a-binding-v1.md)。

## 核心结论

1. 新任务 Message.taskId 必须为空，B02 预分配 DomainTask ID 通过扩展携带，与服务端返回 A2A Task.id 显式映射。原 B02 schema 和字段语义不变。
2. 官方 SDK 使用 protobuf 对象和新版 SendMessage/GetTask 方法。本刀使用 SDK 的 JSON-RPC transport 与 route factory，应用 handler 实现权限、去重、状态和结果约束，没有手写冒充 A2A 的私有 REST 接口。
3. SendMessage.metadata 可装载完整 B02 命令。CancelTask 无 metadata，采用协商的 X-Tiki-Command 扩展头，保留原命令幂等与 revision，不假称标准字段。
4. 标准 SDK 不保证业务幂等或用户隔离。测试在 handler 内校验可信 fixture 主体、资源引用、JCS hash、唯一键及状态；生产必须接入 J01/J04/J07。
5. 订阅断开不等于任务终止。后台 fixture 继续完成，客户端 GetTask 可恢复结果；续订活跃任务可收到结果，终态新输入和终态订阅均按绑定拒绝。

## 覆盖与实测问题

涵盖 ID 映射、正常提交/查询、完整 B02 结果 schema、重复/并发提交、同键异参、更换 command_id、同领域 ID 换键、丢弃 HTTP 响应后重试、input/auth 等待及同任务续接、重复响应、跨用户四类操作拒绝、取消 revision/幂等/冲突、完成与取消顺序、流式工件、断流查询、未知技能/扩展、伪造身份/actor/audience/hash、缺失认证及 JCS 键序和数字格式。

修复并复测的实施问题：protobuf Struct 不支持普通字典 pop；SDK 客户端订阅方法名是 subscribe；Windows 跨版本子进程继承 PYTHONHOME 会加载错误标准库。现在运行器清理相关继承变量。负例只接受预期标准错误类型，内部错误不能算通过。

## 边界及后续

- 测试身份是固定一次性 token 表，不是 B03 OIDC 集成，不验证令牌到期/撤销或 authentication_reference 的真实性。
- 任务与去重仅内存保存，进程退出即丢失；响应丢弃重试只证明存活进程内去重，没有声称崩溃恢复。
- read 技能返回 B02 的静态示例工件，hash 是样例占位，不是真实简历内容 hash；真实读取与证据一致性属于 J06。
- 本刀验证自定义 handler 与 SDK wire 层，不验证 SDK 默认 AgentExecutor/TaskStore 的业务恢复语义。
- 完整生产依赖未改动。锁文件是独立协议工程；尤其 Starlette 1.9.0 / Pydantic 2.14.1 与 JobPilot 现有 FastAPI 的组合需在 J07 单独解析和回归。
- J04 实现事务去重、任务 ID 映射及保留策略；J07 实现生产身份/业务错误/取消核对；T05 持久保存远端映射、有限重试和恢复查询。J01/T01 等 M1 工作仍未开始。
- HTTP 仅绑定 loopback；生产认证请求必须使用受保护连接。无 webhook、gRPC 或 REST binding 验证。

CI 已配置双向版本矩阵，本地成功不能替代 GitHub CI；远程结果以对应提交的 GitHub Actions 为准。服务端进程由运行器结束，原工作区改动未动。
