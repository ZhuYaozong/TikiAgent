"""创建并清理本刀专属容器；拒绝复用已有容器或占用端口。"""
from pathlib import Path
import socket
import subprocess
import sys
import time

from lease_store import validated_dsn

ROOT = Path(__file__).resolve().parent
COMPOSE = ROOT.parents[2] / "deploy/platform/spikes/checkpoint.compose.yaml"


def main():
    validated_dsn()
    command = ["docker", "compose", "-f", str(COMPOSE)]
    existing = subprocess.check_output(command + ["ps", "-aq"], text=True).strip()
    if existing:
        raise RuntimeError("已有 B04 容器，拒绝覆盖")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 38445))
    output = ROOT / ".runtime" / "results.json"
    if output.exists():
        output.rename(output.with_name(f"previous-{time.time_ns()}.json"))
    try:
        subprocess.run(command + ["up", "-d", "--wait", "--wait-timeout", "60"], check=True)
        subprocess.run([sys.executable, str(ROOT / "verify.py")], check=True, timeout=180)
    finally:
        subprocess.run(command + ["down"], check=True)


if __name__ == "__main__":
    main()
