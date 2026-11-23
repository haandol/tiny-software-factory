"""앱 전체가 함께 쓰는 `codex app-server` 하나: 시작할 때 띄우고, 죽으면 다시 띄우고, 앱이 끝날 때 함께 끝낸다.

요청마다 thread/start로 스레드를 하나 만들고 turn/start로 turn을 하나 돌린다.
여러 요청이 동시에 와도 같은 프로세스에서 스레드마다 turn을 하나씩 돌린다.
모델은 지정하지 않는다 (CODEX_HOME 설정을 따른다).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from pathlib import Path

from .config import log
from .rpc import CLOSED, AppServerError, Connection

# 스레드 설정: 읽기 전용 샌드박스, 승인 요청 없음, 기록을 남기지 않는 임시 스레드
THREAD_PARAMS = {"sandbox": "read-only", "approvalPolicy": "never", "ephemeral": True}
MAX_BACKOFF_S = 60.0


class CodexAppServer:
    def __init__(self):
        self._state = threading.Lock()           # _conn, _closed, _restarting
        self._spawn_lock = threading.Lock()      # 동시에 둘 이상 띄우지 않는다
        self._conn: Connection | None = None
        self._closed = False
        self._restarting = False
        self._stderr_path: Path | None = None
        self._backoff = 1.0
        self._not_before = 0.0

    def start(self, stderr_path: Path | None = None) -> None:
        """앱이 시작할 때 부른다. 실패해도 앱은 뜨고, 뒤에서 다시 띄운다."""
        self._stderr_path = stderr_path
        try:
            self.connection()
        except Exception as e:                    # noqa: BLE001
            log(f"codex app-server를 띄우지 못했다: {e}")
            self._schedule_restart()

    @property
    def pid(self) -> int | None:
        conn = self._conn
        return conn.pid if conn and conn.alive else None

    def connection(self) -> Connection:
        """살아 있는 연결. 없으면 새로 띄운다."""
        with self._state:
            if self._closed:
                raise AppServerError("앱이 종료 중이다")
            if self._conn and self._conn.alive:
                return self._conn
        with self._spawn_lock:
            with self._state:
                if self._conn and self._conn.alive:
                    return self._conn
            return self._spawn()

    def _spawn(self) -> Connection:
        time.sleep(max(0.0, self._not_before - time.time()))
        codex = shutil.which("codex")
        if not codex:
            self._delay_next_start()
            raise AppServerError("codex CLI를 찾을 수 없다")
        conn = Connection(codex, self._stderr_path, self._on_exit)
        try:
            info = conn.handshake()
        except Exception:
            conn.close()
            self._delay_next_start()
            raise
        with self._state:
            closed = self._closed
            if not closed:
                self._conn = conn
        if closed:
            conn.close()
            raise AppServerError("앱이 종료 중이다")
        log(f"codex app-server 시작 pid {conn.pid} (pgid {os.getpgid(conn.pid)}, codexHome {info.get('codexHome')})")
        return conn

    def _delay_next_start(self) -> None:
        """띄우기에 실패하거나 띄우자마자 죽으면 점점 늦게 다시 띄운다."""
        self._not_before = time.time() + self._backoff
        self._backoff = min(MAX_BACKOFF_S, self._backoff * 2)

    def _on_exit(self, conn: Connection) -> None:
        if self._closed:
            return
        log(f"{conn.dead} → 다시 띄운다")
        if time.time() - conn.started < 30:
            self._delay_next_start()
        else:
            self._backoff = 1.0
        self._schedule_restart()

    def _schedule_restart(self) -> None:
        with self._state:
            if self._closed or self._restarting:
                return
            self._restarting = True
        threading.Thread(target=self._restart_loop, daemon=True, name="codex-app-server-restart").start()

    def _restart_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    self.connection()
                    return
                except Exception as e:            # noqa: BLE001 - 다음 시도는 _not_before 뒤에 한다
                    log(f"codex app-server 다시 띄우기 실패: {e}")
        finally:
            with self._state:
                self._restarting = False

    def close(self) -> None:
        """앱이 끝날 때 부른다. 다시 띄우지 않는다."""
        with self._state:
            self._closed = True
            conn, self._conn = self._conn, None
        if conn:
            conn.close()

    def run_turn(self, items: list[dict], output_schema: dict, cwd: Path, timeout: float,
                 events_log: Path | None = None, cancel: threading.Event | None = None) -> str:
        """새 스레드에서 turn 하나를 돌리고 마지막 답(agentMessage) 텍스트를 돌려준다.
        cancel이 켜지면 turn을 중단시키고 TurnCancelled를 낸다."""
        deadline = time.time() + timeout
        conn = self.connection()
        thread = conn.request("thread/start", {"cwd": str(cwd), **THREAD_PARAMS})["thread"]
        tid = thread["id"]
        q = conn.subscribe(tid)
        events = [f"# app-server pid {conn.pid}, thread {tid}, model {thread.get('model')}"]
        try:
            started = conn.request("turn/start", {"threadId": tid, "input": items, "outputSchema": output_schema})
            turn = _Turn(started["turn"]["id"])
            return _wait(conn, q, tid, turn, _Limits(deadline, timeout, cancel), events)
        finally:
            conn.unsubscribe(tid)
            conn.request_quietly("thread/unsubscribe", {"threadId": tid})
            if events_log:
                events_log.write_text("\n".join(events) + "\n")


class TurnCancelled(AppServerError):
    """호출한 쪽이 turn을 더 기다리지 않기로 했다 (예: 다른 측정값이 이미 충분히 모였다)."""


class _Limits:
    """turn 하나를 기다리는 한도: 마감 시각과 취소 신호."""

    def __init__(self, deadline: float, timeout: float, cancel: threading.Event | None):
        self.deadline, self.timeout, self.cancel = deadline, timeout, cancel

    def check(self) -> float:
        """남은 시간(초). 시간을 넘겼거나 취소됐으면 예외를 낸다."""
        if self.cancel is not None and self.cancel.is_set():
            raise TurnCancelled("Codex turn을 취소했다")
        left = self.deadline - time.time()
        if left <= 0:
            raise AppServerError(f"Codex turn이 {self.timeout:.0f}초 안에 끝나지 않았다")
        return left


def _wait(conn: Connection, q: queue.Queue, tid: str, turn: "_Turn", limits: _Limits, events: list[str]) -> str:
    """turn이 끝날 때까지 그 스레드의 알림을 읽는다. 시간을 넘기거나 취소되면 turn을 중단시키고 실패로 돌린다."""
    while True:
        try:
            left = limits.check()
        except AppServerError:
            conn.request_quietly("turn/interrupt", {"threadId": tid, "turnId": turn.id})
            raise
        try:
            msg = q.get(timeout=min(left, 1.0))
        except queue.Empty:
            continue
        method, params = msg.get("method"), msg.get("params") or {}
        if params.get("turnId") not in (None, turn.id):
            continue
        events.append(event_line(method, params))
        text = turn.feed(method, params)
        if text is not None:
            return text


class _Turn:
    """turn 하나의 알림을 모아 마지막 답을 고른다 (final_answer가 있으면 그것, 없으면 마지막 agentMessage)."""

    def __init__(self, turn_id: str):
        self.id = turn_id
        self.final: str | None = None
        self.answer: str | None = None
        self.errors: list[str] = []

    def feed(self, method: str, params: dict) -> str | None:
        """알림 하나를 반영한다. turn이 끝났으면 답을 돌려주고, 실패로 끝났으면 예외를 낸다."""
        if method == CLOSED:
            raise AppServerError(params.get("reason", "codex app-server 연결이 끊겼다"))
        if method == "item/completed":
            self._item(params.get("item") or {})
        elif method == "error" and not params.get("willRetry"):
            self.errors.append(str((params.get("error") or {}).get("message", "")))
        elif method == "turn/completed" and (params.get("turn") or {}).get("id") in (None, self.id):
            return self._completed(params["turn"])
        return None

    def _item(self, item: dict) -> None:
        if item.get("type") != "agentMessage" or not item.get("text"):
            return
        if item.get("phase") == "final_answer":
            self.final = item["text"]
        else:
            self.answer = item["text"]

    def _completed(self, t: dict) -> str:
        if t.get("status") != "completed":
            why = (t.get("error") or {}).get("message") or "; ".join(self.errors) or "이유 없음"
            raise AppServerError(f"Codex turn이 {t.get('status')} 상태로 끝났다: {why[:300]}")
        if not (self.final or self.answer):          # 알림을 놓쳤으면 turn에 실린 항목에서 찾는다
            for item in t.get("items") or []:
                self._item(item)
        text = self.final or self.answer
        if not text:
            raise AppServerError("Codex turn이 답 없이 끝났다" + (f": {'; '.join(self.errors)}" if self.errors else ""))
        return text


def event_line(method: str, params: dict) -> str:
    """codex.log 한 줄: 알림 이름과 짧은 내용."""
    item = params.get("item") or {}
    if method in ("item/started", "item/completed"):
        detail = item.get("type", "")
        if method == "item/completed" and item.get("type") in ("agentMessage", "commandExecution"):
            detail += f" {item.get('phase') or item.get('command')}: {item.get('text') or item.get('exitCode')}"
    elif method == "turn/completed":
        t = params.get("turn") or {}
        detail = f"{t.get('status')} {t.get('durationMs')}ms {json.dumps(t.get('error'), ensure_ascii=False)}"
    else:
        detail = json.dumps(params.get("tokenUsage", params), ensure_ascii=False)[:500]
    return f"{time.strftime('%H:%M:%S')} {method} {detail}"
