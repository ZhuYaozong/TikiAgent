"""启动自己的子进程；拒绝复用占用端口，不影响现有服务。"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import httpx

ROOT = Path(__file__).resolve().parent


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 38446))
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    result = runtime / "results.json"
    if result.exists():
        result.rename(runtime / f"previous-{time.time_ns()}.json")
    server_python = os.environ.get("B05_SERVER_PYTHON", sys.executable)
    # 跨 Python 版本启动时不能继承父解释器注入的标准库路径。
    child_env = {key: value for key, value in os.environ.items() if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "__PYVENV_LAUNCHER__"}}
    with (runtime / "server.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen([server_python, "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "38446", "--no-access-log"], cwd=ROOT, stdout=log, stderr=log, env=child_env)
        try:
            with httpx.Client(trust_env=False, timeout=1) as http:
                for _ in range(80):
                    if proc.poll() is not None:
                        raise RuntimeError("server startup failed; inspect .runtime/server.log")
                    try:
                        if http.get("http://127.0.0.1:38446/health").json().get("probe") == "b05":
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    time.sleep(0.1)
                else:
                    raise TimeoutError("server readiness")
            subprocess.run([sys.executable, str(ROOT / "verify.py")], check=True, timeout=120)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)


if __name__ == "__main__":
    main()
