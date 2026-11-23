"""평가 대상 앱: 제품 프로세스(CONTRACT.md 1절) 또는 채점기 검증용 기준 앱."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from ..config import CODEX_HOME, VENDOR
from ..env import agent_env
from .lab import js
from .scenes import EMPTY_SPEC, perturb


@dataclass
class AppHandle:
    base_url: str
    data_dir: Path
    stop: Callable[[], None]
    log_path: Path | None = None
    source_dir: Path | None = None       # 코드 지표를 잴 소스 폴더 (기준 앱은 없음)
    pid: int | None = None


def http(method: str, url: str, body: bytes | None = None, ctype: str | None = None, timeout: float = 30):
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": ctype} if ctype else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, str(e).encode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, proc: subprocess.Popen, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.5)
    return False


def start_product_app(app_dir: Path, data_dir: Path, log_path: Path, port: int | None = None) -> AppHandle:
    """`uv run app.py --port <port> --data-dir <dir>`로 제품을 띄운다."""
    port = port or free_port()
    data_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "a")
    proc = subprocess.Popen(
        ["uv", "run", "app.py", "--port", str(port), "--data-dir", str(data_dir)],
        cwd=app_dir, env=agent_env(CODEX_HOME=str(CODEX_HOME)),
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def stop():
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        log.close()

    if wait_for_port(port, proc, 180):
        return AppHandle(f"http://127.0.0.1:{port}", data_dir, stop, log_path, app_dir, proc.pid)
    code = proc.poll()
    stop()
    reason = f"시작 중에 종료됐다 (exit {code})" if code is not None else f"180초 안에 포트 {port}를 열지 않았다"
    raise RuntimeError(f"app.py가 {reason} (로그 {log_path})")


@dataclass
class CalibrationState:
    """기준 앱 하나의 상태: 종류, 오차 수준, 정답 등록부, 요청 상태."""
    kind: str
    level: float
    registry: dict
    data_dir: Path
    status: dict
    viewer: str
    builder: str

    def resolve(self, image: bytes) -> dict | None:
        spec = self.registry.get(hashlib.sha256(image).hexdigest())
        if self.kind == "empty":
            return EMPTY_SPEC
        if spec is None:
            return None
        return spec if self.kind == "gt" else perturb(spec, self.level)

    def scene_module(self, spec: dict) -> str:
        return (f"const SPEC = {json.dumps(spec)};\n{self.builder}\n"
                "export function build(THREE) { return buildFromSpec(THREE, SPEC, {}); }\n")


class CalibrationHandler(BaseHTTPRequestHandler):
    """CONTRACT.md 2절의 HTTP 주소를 그대로 구현한다. 장면 생성은 요청을 받는 즉시 끝난다."""
    state: CalibrationState

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes | str, ctype: str = "application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/api/requests":
            return self._send(404, "{}")
        st = self.state
        image = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        key = uuid.uuid4().hex[:12]
        spec = st.resolve(image)
        if spec is None:
            st.status[key] = "failed"
        else:
            (st.data_dir / "scenes" / f"{key}.js").write_text(st.scene_module(spec))
            st.status[key] = "done"
        self._send(202, json.dumps({"key": key}))

    def do_GET(self):
        st, path = self.state, urllib.parse.urlparse(self.path).path
        if path.startswith("/api/requests/"):
            key = path.rsplit("/", 1)[-1]
            return self._send(200, json.dumps({"status": st.status[key]})) if key in st.status else self._send(404, "{}")
        if path == "/view":
            return self._send(200, st.viewer, "text/html; charset=utf-8")
        for prefix, folder in (("/scenes/", st.data_dir / "scenes"), ("/vendor/", VENDOR)):
            f = folder / Path(path).name
            if path.startswith(prefix) and f.exists():
                return self._send(200, f.read_bytes(), "text/javascript")
        self._send(404, "{}")


def calibration_app(kind: str, level: float, registry: dict, data_dir: Path) -> AppHandle:
    """채점기 검증용 기준 앱. 계약만 구현하고 Codex는 부르지 않는다.

    gt: 평가기가 등록한 정답 장면을 그대로 돌려준다 (검증 전용)
    empty: 바닥과 벽만 있는 기본 장면을 돌려준다
    noise: 정답에 level만큼 오차를 더해 돌려준다
    """
    (data_dir / "scenes").mkdir(parents=True, exist_ok=True)
    state = CalibrationState(kind, level, registry, data_dir, {}, js("calibration-viewer.html"), js("builder.js"))
    handler = type("Handler", (CalibrationHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return AppHandle(f"http://127.0.0.1:{server.server_port}", data_dir,
                     lambda: (server.shutdown(), server.server_close()))
