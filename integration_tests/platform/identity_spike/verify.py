"""运行真实 Keycloak + Python + Spring Security 场景，报告只保存结论。"""
import json
import platform
import time
from datetime import datetime, timezone

import httpx
from jose import JWTError, jwt

from probe import (BASE, ISSUER, TOKEN, SECRET, HERE, ResourceVerifier, admin, checked,
                   client, enforce_downscope, exchange, login, refresh, renewal_gate)


def main():
    results = []
    verifier = ResourceVerifier()
    java = httpx.Client(base_url="http://127.0.0.1:38444", trust_env=False, timeout=10)

    def record(name, condition, detail=""):
        if not condition:
            raise AssertionError(name + " failed: " + detail)
        results.append({"case": name, "passed": True, "detail": detail})
        print("PASS " + name, flush=True)

    def both(name, token, owner, accepted):
        error_type = "none"
        try:
            verifier.verify(token, owner)
            py_ok = True
        except (JWTError, ValueError, PermissionError) as exc:
            py_ok = False
            error_type = type(exc).__name__
            # 仅输出已知诊断标签，不把 token 或任意异常正文写入日志。
            for marker in ("missing required key", "Signature verification failed", "Signature has expired"):
                if marker in str(exc):
                    error_type += ":" + marker
        status = java.get("/resource/" + owner, headers={"Authorization": "Bearer " + token}).status_code
        record(name, py_ok == accepted and (status == 200 if accepted else status in (401, 403)),
               f"python={py_ok}; java_http={status}; python_error={error_type}")

    versions = checked(java.get("/health")).json()
    a, alice = login("alice")
    b, bob = login("bob")
    record("authorization-code-pkce-two-users", alice != bob)
    ar = checked(exchange(a["access_token"]))
    br = checked(exchange(b["access_token"]))
    at, bt = ar.json(), br.json()
    claims = verifier.verify(at["access_token"], alice)
    record("subject-preserved-actor-bound", claims["sub"] == alice and claims["azp"] == "tiki",
           "actor 从验签后的 azp 映射；没有启用 preview delegation")
    record("exchange-refresh-issued", "refresh_token" in at)
    both("alice-own-resource", at["access_token"], alice, True)
    both("bob-own-resource", bt["access_token"], bob, True)
    both("cross-user-denied", bt["access_token"], alice, False)
    both("wrong-caller-denied", a["access_token"], alice, False)
    record("spring-wrong-issuer-denied", java.get("/probe/wrong-issuer", headers={
        "Authorization": "Bearer " + at["access_token"]}).status_code == 401)
    wrong_aud = checked(exchange(a["access_token"], audience_name="tiki", refresh=False)).json()["access_token"]
    both("wrong-audience-denied", wrong_aud, alice, False)
    no_read = checked(exchange(a["access_token"], scope="resume.write", refresh=False)).json()["access_token"]
    both("missing-required-scope-denied", no_read, alice, False)
    verifier.verify(at["access_token"], alice, online=True)
    record("online-resource-active-user", java.get("/online-resource/" + alice, headers={
        "Authorization": "Bearer " + at["access_token"]}).status_code == 200)
    denied = exchange(a["access_token"], requester="stranger", refresh=False)
    record("untrusted-exchange-client-denied", denied.status_code in (400, 403))
    with client() as c:
        service_token = checked(c.post(TOKEN, data={"grant_type": "client_credentials", "client_id": "tiki",
            "client_secret": SECRET, "scope": "resume.read"})).json()["access_token"]
    both("service-account-cannot-replace-user", service_token, alice, False)
    parts = at["access_token"].split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    both("tampered-signature-denied", ".".join(parts), alice, False)
    # 实际交换结果可能扩大可选 scope；观察供应商行为，并独立证明授权门控拒绝扩大。
    elevated = exchange(a["access_token"], scope="resume.read resume.write")
    grant = {"subject": alice, "scopes": ["resume.read"], "active": True, "deadline": time.time() + 300}
    record("upscope-not-accepted-by-task-gate", renewal_gate(elevated, grant, verifier)["state"] == "WAITING_INPUT",
           f"provider_http={elevated.status_code}; returned_write_scope=" + str(
               "resume.write" in elevated.json().get("scope", "").split()))
    enforce_downscope()
    narrowed = exchange(a["access_token"], scope="resume.read resume.write")
    record("provider-downscope-policy-rejects-expansion", narrowed.status_code == 400 and narrowed.json().get("error") == "invalid_scope")
    scoped = checked(exchange(a["access_token"], refresh=False))
    record("provider-downscope-policy-allows-read", scoped.status_code == 200)
    both("downscoped-token-keeps-subject", scoped.json()["access_token"], alice, True)
    refreshed = checked(refresh(at["refresh_token"]))
    record("background-renewal-no-browser", renewal_gate(refreshed, grant, verifier)["state"] == "RUNNING")
    record("revoked-task-grant-waits", renewal_gate(refreshed, {**grant, "active": False}, verifier)["state"] == "WAITING_INPUT")
    record("expired-task-grant-waits", renewal_gate(refreshed, {**grant, "deadline": 0}, verifier)["state"] == "WAITING_INPUT")
    replay = refresh(at["refresh_token"])
    record("old-refresh-token-replay-denied", replay.status_code == 400)
    record("renewal-failure-authentication-wait", renewal_gate(replay, grant, verifier) == {"state": "WAITING_INPUT", "wait_kind": "authentication"})
    with client() as c:
        headers = admin()
        realm = checked(c.get(BASE + "/admin/realms/b03-spike", headers=headers)).json()
        checked(c.post(BASE + "/admin/realms/b03-spike/components", headers=headers, json={
            "name": "b03-rotated-key-" + str(time.time_ns()), "providerId": "rsa-generated",
            "providerType": "org.keycloak.keys.KeyProvider", "parentId": realm["id"],
            "config": {"priority": ["200"], "enabled": ["true"], "active": ["true"],
                       "algorithm": ["RS256"], "keySize": ["2048"]}}), 201)
    fresh, _ = login("alice")
    rotated = checked(exchange(fresh["access_token"])).json()
    record("new-signing-kid", jwt.get_unverified_header(rotated["access_token"])["kid"] != jwt.get_unverified_header(at["access_token"])["kid"])
    both("rotation-new-key-auto-refresh", rotated["access_token"], alice, True)
    both("rotation-old-key-overlap", bt["access_token"], bob, True)
    # 关闭用户后分别观察本地 JWT 校验与 IdP 在线检查，不能把两者混为一谈。
    fresh_b, _ = login("bob")
    disable_tokens = checked(exchange(fresh_b["access_token"])).json()
    with client() as c:
        headers = admin()
        checked(c.put(BASE + "/admin/realms/b03-spike/users/" + bob, headers=headers, json={"enabled": False}), 204)
        try:
            both("disabled-user-offline-jwt-still-valid", disable_tokens["access_token"], bob, True)
            online = checked(c.post(TOKEN + "/introspect", data={"client_id": "tiki", "client_secret": SECRET,
                "token": disable_tokens["access_token"]})).json()
            record("disabled-user-introspection-inactive", online.get("active") is False)
            try:
                verifier.verify(disable_tokens["access_token"], bob, online=True)
                denied_online = False
            except PermissionError:
                denied_online = True
            record("disabled-user-both-online-resources-deny", denied_online and java.get("/online-resource/" + bob,
                headers={"Authorization": "Bearer " + disable_tokens["access_token"]}).status_code == 401)
            denied_refresh = refresh(disable_tokens["refresh_token"])
            record("disabled-user-renewal-denied", denied_refresh.status_code == 400)
            record("disabled-user-authentication-wait", renewal_gate(denied_refresh,
                {**grant, "subject": bob}, verifier)["state"] == "WAITING_INPUT")
        finally:
            checked(c.put(BASE + "/admin/realms/b03-spike/users/" + bob, headers=admin(), json={"enabled": True}), 204)
    # 用同一真实签名令牌测试 Python 的 issuer 检查，不修改签名内容。
    try:
        jwt.decode(rotated["access_token"], verifier.keys, algorithms=["RS256"], audience="jobpilot", issuer=ISSUER + "-wrong")
        issuer_denied = False
    except JWTError:
        issuer_denied = True
    record("python-wrong-issuer-denied", issuer_denied)
    exp = jwt.get_unverified_claims(rotated["access_token"])["exp"]
    wait = max(0, exp - time.time() + 2)
    print(f"等待真实令牌到期，最多 {wait:.0f} 秒", flush=True)
    while wait > 0:
        time.sleep(min(wait, 10))
        wait = max(0, exp - time.time() + 2)
    both("expired-token-denied", rotated["access_token"], alice, False)
    record("expired-subject-exchange-denied", exchange(rotated["access_token"], refresh=False).status_code == 400)
    session_login, _ = login("alice")
    session_tokens = checked(exchange(session_login["access_token"])).json()
    with client() as c:
        checked(c.post(BASE + "/admin/realms/b03-spike/users/" + alice + "/logout", headers=admin()), 204)
    record("sso-session-removed-renewal-waits", renewal_gate(refresh(session_tokens["refresh_token"]),
        {**grant, "deadline": time.time() + 60}, verifier)["state"] == "WAITING_INPUT")
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "keycloak": "26.8.1",
              "versions": {**versions, "python": platform.python_version()}, "cases": results, "count": len(results), "all_passed": True}
    (HERE / ".runtime/results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"PASS B03 {len(results)} cases; sanitized report saved", flush=True)
    java.close()


if __name__ == "__main__":
    main()
