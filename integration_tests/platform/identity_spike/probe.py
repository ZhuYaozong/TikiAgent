"""B03 实际 OIDC 协议辅助程序；只允许固定本机测试 issuer。"""
import base64
import hashlib
import secrets
import ssl
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from jose import jwt

HERE = Path(__file__).resolve().parent
BASE = "https://localhost:38443"
ISSUER = BASE + "/realms/b03-spike"
TOKEN = ISSUER + "/protocol/openid-connect/token"
SECRET = "disposable-b03-client"
PASSWORD = "disposable-b03-user"
TLS = ssl.create_default_context(cafile=str(HERE / ".runtime/certs/tls.crt"))


def client():
    class LoopbackClient(httpx.Client):
        def request(self, method, url, **kwargs):
            # 在 Cookie 处理之前固定网络地址；issuer 仍严格验证 localhost。
            parsed = httpx.URL(url)
            if parsed.host != "localhost" or parsed.port != 38443 or parsed.scheme != "https":
                raise ValueError("探针只允许固定本机 IdP")
            return super().request(method, parsed.copy_with(host="127.0.0.1"), **kwargs)
    return LoopbackClient(verify=TLS, trust_env=False, timeout=20)


def checked(response, status=200):
    # 不输出响应正文，防止失败日志泄漏 access/refresh token。
    if response.status_code != status:
        kind = response.json().get("error", "unknown") if "json" in response.headers.get("content-type", "") else "non-json"
        raise AssertionError(f"HTTP {response.status_code}, expected {status}, error={kind}")
    return response


def admin():
    with client() as c:
        token = checked(c.post(BASE + "/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli", "username": "spike-admin",
            "password": "disposable-b03-admin"})).json()["access_token"]
    return {"Authorization": "Bearer " + token}


def audience(name):
    return {"name": "aud-" + name, "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": name, "access.token.claim": "true", "id.token.claim": "false"}}


def configure():
    """仅创建新的临时 realm；存在时拒绝覆盖，避免误删运行数据。"""
    with client() as c:
        for _ in range(60):
            try:
                if c.get(BASE + "/realms/master").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        headers = admin()
        scopes = [{"name": name, "protocol": "openid-connect", "attributes": {"include.in.token.scope": "true"},
                   "protocolMappers": [audience("tiki"), audience("jobpilot")]} for name in ["resume.read", "resume.write"]]
        # 只减权策略过滤 scope 时必须保留基础主体映射；不放在客户端专有 mapper 中。
        scopes.append({"name": "b03-identity", "protocol": "openid-connect", "attributes": {"include.in.token.scope": "false"},
            "protocolMappers": [{"name": "subject", "protocol": "openid-connect", "protocolMapper": "oidc-sub-mapper",
                "config": {"access.token.claim": "true", "introspection.token.claim": "true"}}]})
        clients = []
        for name in ["platform", "tiki", "jobpilot", "stranger"]:
            clients.append({"clientId": name, "secret": SECRET, "publicClient": False,
                "protocol": "openid-connect", "enabled": True, "fullScopeAllowed": False,
                "standardFlowEnabled": name == "platform", "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": name == "tiki",
                "redirectUris": ["http://127.0.0.1:38445/callback"],
                "defaultClientScopes": ["b03-identity"], "optionalClientScopes": ["resume.read", "resume.write"],
                "attributes": {"standard.token.exchange.enabled": "true" if name == "tiki" else "false",
                    "standard.token.exchange.enableRefreshRequestedTokenType": "SAME_SESSION",
                    "pkce.code.challenge.method": "S256" if name == "platform" else ""}})
        users = [{"username": name, "firstName": name, "lastName": "Fixture", "email": name + "@example.invalid",
                  "emailVerified": True, "enabled": True, "credentials": [{"type": "password", "value": PASSWORD, "temporary": False}]}
                 for name in ["alice", "bob"]]
        checked(c.post(BASE + "/admin/realms", headers=headers, json={"realm": "b03-spike", "enabled": True,
            "sslRequired": "all", "registrationAllowed": False, "accessTokenLifespan": 45,
            "ssoSessionIdleTimeout": 300, "ssoSessionMaxLifespan": 600, "revokeRefreshToken": True,
            "refreshTokenMaxReuse": 0, "clientScopes": scopes, "clients": clients, "users": users}), 201)


class LoginForm(HTMLParser):
    """只解析测试 IdP 登录页的 form action 和隐藏字段。"""
    def __init__(self):
        super().__init__()
        self.action = None
        self.hidden = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "kc-form-login":
            self.action = attrs.get("action")
        if tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
            self.hidden[attrs["name"]] = attrs.get("value", "")


def login(username):
    """真实授权码 + PKCE + state + nonce；用户密码仅发送到本机 Keycloak 表单。"""
    verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect = "http://127.0.0.1:38445/callback"
    with client() as c:
        response = checked(c.get(ISSUER + "/protocol/openid-connect/auth", params={
            "client_id": "platform", "response_type": "code", "redirect_uri": redirect, "scope": "openid resume.read",
            "code_challenge_method": "S256", "code_challenge": challenge, "state": state, "nonce": nonce}))
        form = LoginForm()
        form.feed(response.text)
        if not form.action or not form.action.startswith(ISSUER + "/login-actions/"):
            raise AssertionError("登录表单不属于本机测试 issuer")
        response = checked(c.post(form.action, data={**form.hidden, "username": username, "password": PASSWORD}), 302)
        location = response.headers["location"]
        if not location.startswith(redirect + "?"):
            raise AssertionError("回调地址不匹配")
        query = parse_qs(urlparse(location).query)
        assert query["state"] == [state]
        tokens = checked(c.post(TOKEN, data={"grant_type": "authorization_code", "client_id": "platform", "client_secret": SECRET,
            "code": query["code"][0], "code_verifier": verifier, "redirect_uri": redirect})).json()
        jwks = checked(c.get(ISSUER + "/protocol/openid-connect/certs")).json()
        claims = jwt.decode(tokens["id_token"], jwks, algorithms=["RS256"], audience="platform", issuer=ISSUER,
                            access_token=tokens["access_token"])
        assert claims["nonce"] == nonce
        return tokens, claims["sub"]


def exchange(token, scope="resume.read", audience_name="jobpilot", requester="tiki", refresh=True):
    with client() as c:
        return c.post(TOKEN, data={"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": requester, "client_secret": SECRET, "subject_token": token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:" + ("refresh_token" if refresh else "access_token"),
            "audience": audience_name, "scope": scope})


def refresh(token):
    with client() as c:
        return c.post(TOKEN, data={"grant_type": "refresh_token", "client_id": "tiki", "client_secret": SECRET, "refresh_token": token})


def enforce_downscope():
    """通过真实管理 API 开启标准交换只减权；只影响本次临时 realm。"""
    with client() as c:
        headers = admin()
        checked(c.put(BASE + "/admin/realms/b03-spike/client-policies/profiles", headers=headers, json={
            "profiles": [{"name": "b03-downscope", "executors": [{
                "executor": "downscope-assertion-grant-enforcer", "configuration": {}}]}]}), 204)
        checked(c.put(BASE + "/admin/realms/b03-spike/client-policies/policies", headers=headers, json={
            "policies": [{"name": "b03-exchange-policy", "enabled": True, "conditions": [{
                "condition": "grant-type", "configuration": {"grant_types": ["urn:ietf:params:oauth:grant-type:token-exchange"]}}],
                "profiles": ["b03-downscope"]}]}), 204)


class ResourceVerifier:
    """模拟 Python 资源端：固定 issuer/JWKS，未知 kid 刷新，不接受令牌自报 URL。"""
    def __init__(self):
        self.keys = {"keys": []}

    def verify(self, token, owner, online=False):
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise ValueError("算法不允许")
        if header.get("kid") not in {key["kid"] for key in self.keys["keys"]}:
            with client() as c:
                self.keys = checked(c.get(ISSUER + "/protocol/openid-connect/certs")).json()
        claims = jwt.decode(token, self.keys, algorithms=["RS256"], audience="jobpilot", issuer=ISSUER,
                            options={"require_exp": True, "require_sub": True})
        if claims.get("azp") != "tiki" or "resume.read" not in claims.get("scope", "").split():
            raise ValueError("调用方或 scope 不允许")
        if claims["sub"] != owner:
            raise PermissionError("资源主体不匹配")
        if online:
            with client() as c:
                active = checked(c.post(TOKEN + "/introspect", data={"client_id": "jobpilot",
                    "client_secret": SECRET, "token": token})).json().get("active")
            if active is not True:
                raise PermissionError("用户或会话已失效")
        return claims


def renewal_gate(response, grant, verifier):
    """参考任务授权门控：仅代表当前可信夹具事实，不是生产授权账本。"""
    if not grant["active"] or time.time() >= grant["deadline"] or response.status_code != 200:
        return {"state": "WAITING_INPUT", "wait_kind": "authentication"}
    try:
        claims = verifier.verify(response.json()["access_token"], grant["subject"])
        scopes = set(claims.get("scope", "").split()) - {"openid", "profile", "email"}
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        if not scopes <= set(grant["scopes"]) or set(audiences) != {"jobpilot"}:
            raise ValueError("续期不能扩大任务授权")
    except (ValueError, PermissionError, KeyError, jwt.JWTError):
        return {"state": "WAITING_INPUT", "wait_kind": "authentication"}
    return {"state": "RUNNING"}
