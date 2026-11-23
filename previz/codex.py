"""Codex 측정 호출: 앱이 함께 쓰는 codex app-server(previz/app_server.py)에 이미지와 지시문을 turn 하나로 보내
출력 스키마(ANALYSIS_SCHEMA)를 따르는 측정값 JSON을 받는다.

measure()는 같은 이미지·지시문으로 turn 여러 개를 동시에(스레드마다 하나) 돌려 측정값을 모은다.
Codex의 픽셀 측정은 잴 때마다 흔들리므로, 그 가운데 하나를 previz/consensus.py가 고른다.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from .app_server import CodexAppServer, TurnCancelled
from .config import CODEX_SAMPLE_GRACE_S, CODEX_TIMEOUT_S, log
from .prompt import ANALYSIS_SCHEMA, PROMPT

CODEX = CodexAppServer()          # 앱이 시작할 때 cli.main이 띄우고, 끝날 때 닫는다


def run_codex(image_path: Path, work: Path, W: int, H: int, sample: int = 1,
              cancel: threading.Event | None = None) -> dict:
    """Codex에 이미지 하나를 보여 주고 측정값 JSON을 받는다. 입력 순서는 이미지 → 지시문."""
    started = time.time()
    items = [{"type": "localImage", "path": str(image_path.resolve())},
             {"type": "text", "text": PROMPT.format(W=W, H=H)}]
    text = CODEX.run_turn(items, ANALYSIS_SCHEMA, cwd=work, timeout=CODEX_TIMEOUT_S,
                          events_log=work / f"codex-{sample}.log", cancel=cancel)
    data = _parse_json(text)
    log(f"codex 완료 {image_path.parent.name} #{sample} {time.time() - started:.0f}초")
    return data


def measure(image_path: Path, work: Path, W: int, H: int, n: int) -> list[tuple[int, dict]]:
    """turn n개를 동시에 돌려 (표본 번호, 측정값) 목록을 받는다. 일부가 실패해도 나머지로 진행한다.
    과반이 모인 뒤에도 끝나지 않은 turn은 CODEX_SAMPLE_GRACE_S만 더 기다리고 취소한다. 모두 실패하면 예외를 낸다."""
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix=f"codex-{image_path.parent.name}") as ex:
        futures = {ex.submit(run_codex, image_path, work, W, H, i, cancel): i for i in range(1, n + 1)}
        results, errors = _collect(futures, cancel, quorum=math.ceil(n / 2))
    if not results:
        raise RuntimeError(errors[0] if errors else "Codex 측정값을 하나도 받지 못했다")
    for e in errors:
        log(f"codex 표본 실패 {image_path.parent.name}: {e[:200]}")
    return sorted(results)


def _collect(futures: dict[Future, int], cancel: threading.Event, quorum: int) -> tuple[list, list[str]]:
    """끝나는 대로 결과를 모은다. 성공이 quorum개가 되면 그때부터 CODEX_SAMPLE_GRACE_S 뒤에 나머지를 취소한다."""
    results, errors, pending = [], [], set(futures)
    grace_until = None
    while pending:
        timeout = None if grace_until is None or cancel.is_set() else max(0.0, grace_until - time.time())
        done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
        for f in done:
            try:
                results.append((futures[f], f.result()))
            except TurnCancelled:
                pass
            except Exception as e:                 # noqa: BLE001 - 표본 하나의 실패는 기록하고 넘어간다
                errors.append(f"#{futures[f]}: {e}")
        if grace_until is None and len(results) >= quorum:
            grace_until = time.time() + CODEX_SAMPLE_GRACE_S
        if not done and pending:                   # 기다리는 시간이 끝났다: 남은 turn을 취소하고 끝나기를 기다린다
            cancel.set()
    return results, errors


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
