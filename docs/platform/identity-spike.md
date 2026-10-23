# B03：身份提供方与受托身份兼容性验证

日期：2026-10-24。范围：M0 独立验证；不修改现有业务认证，不开始 B04/B05。

## 判断

Keycloak 标准 Token Exchange V2 可作为首期身份候选：本机实测支持授权码登录、保持用户 sub 的目标服务交换、同一用户会话内的 refresh token 和密钥轮换。必须同时启用只减权策略、资源端的 audience/调用方/用户检查，以及应用自己的任务授权门控。

不能仅靠离线 JWT 校验实现用户即时停用。本次同时测得：禁用用户后未过期 JWT 仍通过 Java/Python 本地验签；IdP introspection 返回 inactive，两个在线资源检查入口均拒绝访问，refresh 被拒绝。因此对受保护执行默认采用在线用户/会话状态核对；若未来缓存，必须明确最大撤销延迟，并在故障时暂停而非放行。

## 实际兼容组合

| 项目 | 本次版本 |
|---|---|
| Keycloak | 26.8.1；容器 digest `sha256:a78d6b79572168cb25886b80a8b5b8d4ed4390743e3eecb810538683e209dd6b` |
| Java | Temurin 21.0.12.1+1（独立便携包）及本机 22.0.2；编译目标 21 |
| Spring Boot / Security | 3.5.16 / 6.5.11 |
| Spring Framework / Nimbus JOSE JWT | 6.2.19 / 9.37.4（从构建产物核对） |
| Python | 独立 3.12.10 锁定依赖环境；另在 3.13.12 验证过主体链路 |
| python-jose / cryptography / httpx | 3.5.0 / 48.0.0 / 0.28.1 |
| Maven / Docker | 3.9.0 / 28.4.0 |

这是一组已验证的候选组合，不是对“最新版本”或生产安全审计的承诺。Java 21 为首期 LTS 构建与运行目标。便携 JDK 从 Adoptium 官方 API 返回的发布地址下载并核对 SHA-256，仅放在被忽略的测试目录，未替换系统 Java。没有修改两仓库原有运行依赖或锁文件。

## 身份与授权链路

虚构用户通过真实授权码 + PKCE(S256) 登录 platform，验证 state、ID token 签名/issuer/audience/nonce/at_hash。用户密码只发送给临时 Keycloak 登录表单；应用客户端禁用密码授权。测试 admin-cli 的密码授权只用于初始化本机测试 IdP。

初始用户 token 允许 tiki 作为接收方；tiki 以自己的 confidential client 凭据交换 jobpilot audience 的令牌。sub 保持原用户，azp 为 tiki。适配器可在验签并检查 issuer 后把 azp 映射为 B02 actor；这不是完整 RFC 8693 act 委派链，也不是持有者的 mTLS/DPoP 身份证明。首期固定可信调用链，不启用 preview delegation 或 legacy token exchange。

资源端检查签名、RS256、issuer、exp/nbf、audience、azp 和 resume.read，并将 sub 与资源 owner 比较。真实系统还需要 issuer+subject → principal_id/user_id 的持久唯一映射；本工程使用虚构 sub 作为资源标识，没有假装实现 J01 的用户映射表或并发开户。

Keycloak 极简 realm 导入不自动提供所需 sub mapper，本工程显式配置官方 `oidc-sub-mapper`；缺失 sub 的 token 被拒绝。联测发现只减权策略会过滤客户端专有 mapper，已将主体 mapper 放到默认、`include.in.token.scope=false` 的基础 scope 中，并以减权后主体保留场景验证。标准交换的 refresh 开关使用版本源码中的 `standard.token.exchange.enableRefreshRequestedTokenType=SAME_SESSION`。

## 不扩权与续期

实测默认配置允许用只含 resume.read 的初始 token 请求 resume.write。参考任务门控按预先保存的主体、允许 scope、目标 audience、授权有效性和 deadline 拒绝扩权结果；不能相信客户端自报“任务已批准”。随后在 IdP 配置 `downscope-assertion-grant-enforcer`，实测扩权请求返回 invalid_scope，合法只读交换仍成功。

生产需要两层限制：IdP 只减权策略从初始化开始生效；应用执行前及续期前查受保护的任务授权，续期后再次核对返回的主体、audience、scope。当前 `renewal_gate` 只是给定可信事实后的参考决策，不是持久授权服务、并发凭据更新或生产续期协调器。

交换得到的 refresh token 在浏览器客户端关闭后仍可由 tiki 续期，同一旧 refresh token 重放被拒绝。续期失败、授权撤销或任务期限到达，参考状态为 WAITING_INPUT/authentication，不回退到全权服务身份。

SAME_SESSION 不承诺独立于 Keycloak SSO 会话永久运行。实际通过管理 API 清除用户 SSO 会话后，refresh 被拒，门控进入认证等待。本次没有验证 offline token、跨 SSO 最大生命周期的任务续期、浏览器前端 logout 引发的整条撤销传播或长期凭据加密存储。后续需把 refresh 凭据只保存在服务端受保护存储，checkpoint 仅存引用；会话失效时等待重新认证，不把浏览器 cookie 到期直接解释为取消任务。

## 停用、轮换和过期

两个验证器对两名用户本人资源成功、跨用户资源失败。错误调用方/audience/scope、篡改签名和真实到期 token 都被拒绝；issuer 检查使用真实合法签名 token 配合错误期望 issuer 验证。

增加更高优先级 RS256 signing key 后，新的 token kid 改变，Python 与 Spring 自动刷新 JWKS 并接受新签名；保留旧公钥时，旧未到期 token 仍可使用。这验证正常轮换重叠窗口，不代表密钥泄漏后的即时紧急撤销；清空缓存、移除旧 key 和吊销窗口需要另测。

用户停用的在线/离线差异见前述判断。在线核对是实际 IdP 请求，不是 mock；数据库资源 owner 校验则使用内存夹具，真实业务归属和 J01/P02 的入口保护仍未实现。

## 验收和复现

**最终结果：35 个场景全部通过。** 最终验收使用 Temurin 21.0.12.1、Python 3.12.10 和 requirements.lock 精确依赖，实际构建并启动 Spring Security HTTP 服务；从全新 tmpfs realm 运行至自动清理。此前 Java 22 / Python 3.13 的 33 场景验证也通过。

入口：[独立验证工程](../../integration_tests/platform/identity_spike/README.md)。使用全新 tmpfs realm 从头运行；运行器自动停止本次启动的资源服务器并 compose down。既有同名容器/端口占用时拒绝接管。

证据：[脱敏场景结果](identity-spike-results.json)。只保存场景名、结果、HTTP 状态和实际版本，不含 token、密码、私钥或简历。`.runtime` 私钥与本机日志被忽略；CI 只上传脱敏结果。CI 已配置；本地验收结果不等同于远程 CI 通过，远程结果以对应提交的 GitHub Actions 为准。

## 后续任务影响

- J01：持久 issuer+sub 映射，禁止按用户名/邮箱自动认领旧账号；在线状态核对失败不能降级到开发头。
- P02：使用已实测 Java 21 候选组合，实现 BFF 登录/CSRF/安全 cookie、受保护凭据存储和刷新原子更新；生产接入仍须单独验收。
- T01/J04：验证可信 actor/audience 与任务绑定，资源所有权不可由 JWT scope 代替。
- P07/J09 等审批/执行任务：操作授权与身份 token 分开；需要真实事务消费和撤销竞态测试。

本刀未修改 B02 schema；issuer 使用有效 HTTPS 本机地址，现有契约无需为了验证放宽。未开放生产服务、任意远程 Agent 或后台全权账号。

来源：[Keycloak 标准交换](https://www.keycloak.org/securing-apps/token-exchange)、[固定版本配置属性](https://github.com/keycloak/keycloak/blob/26.8.1/server-spi-private/src/main/java/org/keycloak/protocol/oidc/OIDCConfigAttributes.java)、[Spring Security JWT](https://docs.spring.io/spring-security/reference/servlet/oauth2/resource-server/jwt.html)、[Spring Boot 3.5 运行要求](https://docs.spring.io/spring-boot/3.5/system-requirements.html)。产品能力以本工程实际结果为准。
