# B03 身份兼容性验证工程

这是独立验证工程，不是生产认证实现。只使用虚构用户、临时 Keycloak realm 和内存中的资源归属；不读取 TiKiAgent/JobPilot 的业务库或 `.env`。

## 环境与运行

- Docker Compose v2；本机 38443、38444 空闲。
- Java 21+、keytool、Maven 3.9+；本次在 Temurin 21.0.12.1 和 Java 22.0.2 分别实测，编译目标 21。CI 配置使用 Temurin 21，但配置存在不代表远程运行已通过。
- Python 3.12+；使用此目录独立虚拟环境。

仓库根目录执行：

```powershell
python -m venv integration_tests/platform/identity_spike/.venv
integration_tests/platform/identity_spike/.venv/Scripts/python -m pip install --require-hashes -r integration_tests/platform/identity_spike/requirements.lock
# Maven 不在 PATH 时设置其完整路径；Java/keytool 仍需在 PATH。
# $env:B03_MAVEN = 'D:\maven\apache-maven-3.9.0-bin\apache-maven-3.9.0\bin\mvn.cmd'
integration_tests/platform/identity_spike/.venv/Scripts/python -u integration_tests/platform/identity_spike/run.py
```

Linux/macOS 使用虚拟环境的 `bin/python`。首次运行下载固定 Keycloak 镜像和 Maven 依赖。脚本创建仅本机信任的 7 天 TLS 测试证书及独立 Java truststore；不会关闭证书校验或修改系统信任。

`run.py` 构建 Java → 启动临时 IdP → 创建 realm → 启动 Java 资源服务器 → 运行验证 → 在 finally 中停止本次 Java 与容器。Keycloak 使用 tmpfs，关闭容器后丢弃测试身份数据。已有同名容器或资源端口占用时拒绝接管，避免破坏其他运行。不要并发运行两份。

成功输出 `PASS B03 ... cases`。脱敏结果在 `.runtime/results.json`，前次结果在重跑前归档；不会保存完整 token。私钥、truststore、Java 本机日志均在被 Git 忽略的 `.runtime/`，不能上传整个目录。CI 只上传脱敏 JSON。

进程被外部强制终止时，核对后可手工停止本工程容器：

```powershell
docker compose -f deploy/platform/spikes/identity.compose.yaml down
```

证书过期后使用新的独立工作区运行；不要把开发测试证书、固定示例密码或 start-dev 配置用于部署。38445 仅作为授权码回调地址，探针拦截重定向，不实际启动回调 HTTP 服务。

## 验证结构

- `prepare.py`：生成本机临时 TLS 材料。
- `probe.py`：实际授权码/PKCE/state/nonce/at_hash、exchange、refresh、JWT、introspection 及任务授权参考门控。
- `verify.py`：用实际 token 驱动两用户、错误身份、续期、重放、停用、换钥与到期场景。
- `java/`：Spring Security JWT 资源服务器，离线及在线校验入口。它模拟 JobPilot 资源保护语义，验证 Java 栈能消费同一协议，不是 Java 平台业务服务。
- `requirements.txt` 固定直接验证依赖，`requirements.lock` 固定传递依赖与摘要；Java 通过固定 Spring Boot BOM 管理依赖，Keycloak 同时锁定 tag 与 image digest。

测试先刻意观察 IdP 未启用只减权策略时的行为，再通过管理 API 开启 `downscope-assertion-grant-enforcer` 并验证拒绝扩权。此临时配置过程只供兼容性研究；生产初始化必须从第一刻启用相应策略。

结论、实际版本及尚未覆盖的边界见 [B03 验证报告](../../../docs/platform/identity-spike.md)。
