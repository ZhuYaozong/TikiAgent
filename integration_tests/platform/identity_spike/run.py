"""一键运行独立 B03 探针，停止自身启动的 Java 和 Compose，不接触业务服务。"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
COMPOSE = ROOT / "deploy/platform/spikes/identity.compose.yaml"


def main():
    maven = os.environ.get("B03_MAVEN") or shutil.which("mvn") or shutil.which("mvn.cmd")
    java = shutil.which("java")
    keytool = shutil.which("keytool")
    if not all((maven, java, keytool, shutil.which("docker"))):
        raise SystemExit("需要 Java 21+、keytool、Maven、Docker；可用 B03_MAVEN 指定 Maven 路径")
    with socket.socket() as check:
        if check.connect_ex(("127.0.0.1", 38444)) == 0:
            raise SystemExit("38444 已被使用，拒绝复用未知资源服务器")
    def run(args, **kwargs):
        subprocess.run(args, check=True, cwd=ROOT, **kwargs)
    # 重跑前不销毁已有 realm/数据；使用独立临时工作区或明确清理后重建。
    if not (HERE / ".runtime/certs/tls.crt").exists():
        run([sys.executable, str(HERE / "prepare.py")])
    trust = HERE / ".runtime/truststore.p12"
    if not trust.exists():
        run([keytool, "-importcert", "-noprompt", "-alias", "b03-local", "-file", str(HERE / ".runtime/certs/tls.crt"),
             "-keystore", str(trust), "-storepass", "disposable-b03-trust", "-storetype", "PKCS12"])
    run([maven, "-B", "-q", "-f", str(HERE / "java/pom.xml"), "package"])
    proc = None
    # 已存在同名容器时拒绝接管，避免清理其他人的运行。
    existing = subprocess.check_output(["docker", "compose", "-f", str(COMPOSE), "ps", "-a", "-q"], text=True)
    if existing.strip():
        raise SystemExit("B03 容器已存在，请先核对上次运行，再自行决定是否 compose down")
    report = HERE / ".runtime/results.json"
    if report.exists():
        report.rename(report.with_name("results.previous." + str(time.time_ns()) + ".json"))
    try:
        run(["docker", "compose", "-f", str(COMPOSE), "up", "-d"])
        from probe import configure
        configure()
        with (HERE / ".runtime/java.log").open("w", encoding="utf-8") as log:
            proc = subprocess.Popen([java, "-Djavax.net.ssl.trustStore=" + str(trust),
                "-Djavax.net.ssl.trustStorePassword=disposable-b03-trust", "-jar",
                str(HERE / "java/target/identity-spike-1.0.0.jar")], stdout=log, stderr=log)
            for _ in range(60):
                if proc.poll() is not None:
                    raise RuntimeError("Java 探针未启动，请查看本机日志")
                try:
                    if httpx.get("http://127.0.0.1:38444/health", trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Java 探针启动超时")
            run([sys.executable, "-u", str(HERE / "verify.py")])
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        run(["docker", "compose", "-f", str(COMPOSE), "down"])


if __name__ == "__main__":
    main()
