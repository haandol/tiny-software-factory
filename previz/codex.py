"""Codex 헤드리스 호출: 이미지 하나를 측정값 JSON으로 받고, 실행 중인 Codex 프로세스를 추적해 종료 때 정리한다."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import CODEX_TIMEOUT_S, log
from .prompt import ANALYSIS_SCHEMA, PROMPT


def run_codex(image_path: Path, work: Path, W: int, H: int) -> dict:
    """Codex를 헤드리스로 한 번 호출해 측정값 JSON을 받는다. 환경 변수는 그대로 넘긴다."""
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI를 찾을 수 없다")
    schema_path = work / "schema.json"
    schema_path.write_text(json.dumps(ANALYSIS_SCHEMA))
    out_path = work / "codex-last-message.json"
    out_path.unlink(missing_ok=True)
    cmd = [codex, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
           "-s", "read-only", "-C", str(work), "--output-schema", str(schema_path),
           "-o", str(out_path), "-i", str(image_path)]          # 프롬프트는 stdin으로 넘긴다
    started = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=os.environ.copy(), cwd=str(work))
    RUNNING.add(proc)
    try:
        stdout, stderr = proc.communicate(PROMPT.format(W=W, H=H), timeout=CODEX_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"codex exec가 {CODEX_TIMEOUT_S}초 안에 끝나지 않았다")
    finally:
        RUNNING.discard(proc)
    (work / "codex.log").write_text(f"$ {' '.join(cmd)}\n--- stdout\n{stdout}\n--- stderr\n{stderr}\n")
    if proc.returncode != 0:
        raise RuntimeError(f"codex exec 실패 (exit {proc.returncode}): {(stderr or stdout).strip()[-300:]}")
    text = out_path.read_text() if out_path.exists() else stdout
    data = _parse_json(text)
    log(f"codex 완료 {image_path.parent.name} {time.time() - started:.0f}초")
    return data


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    raise RuntimeError(f"Codex 응답이 JSON이 아니다: {text.strip()[:200]!r}")


class _Running:
    """실행 중인 Codex 프로세스. 앱이 종료될 때 함께 정리한다."""

    def __init__(self):
        self._procs: set = set()
        self._lock = threading.Lock()

    def add(self, p):
        with self._lock:
            self._procs.add(p)

    def discard(self, p):
        with self._lock:
            self._procs.discard(p)

    def kill_all(self):
        with self._lock:
            for p in list(self._procs):
                try:
                    p.kill()
                except OSError:
                    pass


RUNNING = _Running()
