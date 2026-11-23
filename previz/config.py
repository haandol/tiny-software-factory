"""설정: 파일 경로, 업로드·이미지 한도, Codex 호출 시간·횟수·동시성(환경 변수), 키 형식, 로그 출력."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent      # app.py가 있는 폴더
VENDOR = HERE / "vendor"
VENDOR_FILES = {"three.module.js", "three.core.js"}
STATIC = Path(__file__).resolve().parent / "static"

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_IMAGE_SIDE = 1280                     # 큰 이미지는 이 크기로 줄여 Codex에 넘긴다 (기하는 줄인 좌표계로 푼다)
CODEX_TIMEOUT_S = int(os.environ.get("PREVIZ_CODEX_TIMEOUT", "420"))
CODEX_TRIES = int(os.environ.get("PREVIZ_CODEX_TRIES", "2"))
CONCURRENCY = int(os.environ.get("PREVIZ_CONCURRENCY", "8"))
KEY_RE = re.compile(r"^[a-f0-9]{8,32}$")


def log(*parts) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def static_text(name: str) -> str:
    """previz/static/ 아래 정적 파일(뷰어 HTML, 장면 빌더 JS 등)의 내용."""
    return (STATIC / name).read_text(encoding="utf-8")
