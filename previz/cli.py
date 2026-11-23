"""명령행 진입: 인자를 읽고, codex app-server를 띄우고, 저장소·작업자를 만들어 남은 요청을 이어 처리하고, HTTP 서버를 띄운다."""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import signal
from http.server import ThreadingHTTPServer
from pathlib import Path

from .codex import CODEX
from .config import log
from .store import Store
from .web import make_handler
from .worker import Worker


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="tiny previz: 사진 → three.js 프리비즈 장면")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--host", default="127.0.0.1")
    return ap.parse_args()


def _stop(signum, frame):
    """종료 신호: codex app-server를 함께 끝내고 바로 끝낸다."""
    CODEX.close()
    os._exit(0)


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir).resolve()
    store = Store(data_dir)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    worker = Worker(store)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, worker))
    server.daemon_threads = True
    atexit.register(CODEX.close)
    CODEX.start(stderr_path=data_dir / "codex-app-server.log")   # 앱과 같은 프로세스 그룹에 하나 띄운다
    for key in store.pending():                      # 재시작 전에 끝나지 못한 요청을 이어서 처리한다
        log(f"[{key}] 재시작 후 다시 처리")
        worker.submit(key)

    log(f"tiny previz: http://{args.host}:{args.port}  data={data_dir}  codex={shutil.which('codex')} "
        f"app-server pid={CODEX.pid}")
    try:
        server.serve_forever()
    finally:
        CODEX.close()
    return 0
