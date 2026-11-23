"""`codex app-server` 프로세스 하나와의 표준 입출력 JSON-RPC 연결 (한 줄에 메시지 하나).

- 요청은 id로 응답을 기다린다. 알림은 threadId로 그 스레드를 구독한 큐에 넣는다.
- 서버가 보내는 요청(승인 요청 등)은 거절해서 turn이 멈추지 않게 한다.
- 프로세스가 죽으면 기다리던 요청과 구독 큐에 알리고 on_exit를 부른다.
- 앱과 같은 프로세스 그룹에서 실행하고(start_new_session을 쓰지 않는다), 환경 변수는 그대로 넘긴다.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from .config import log

RPC_TIMEOUT_S = 60                       # initialize, thread/start, turn/start 같은 요청의 응답 대기
CLIENT_INFO = {"name": "tiny-previz", "title": "tiny previz", "version": "1"}
# 글자 단위 스트리밍 알림은 쓰지 않으므로 받지 않는다 (완료 알림 item/completed만 쓴다)
OPT_OUT_NOTIFICATIONS = [
    "item/agentMessage/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded", "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
    "item/plan/delta", "command/exec/outputDelta", "process/outputDelta",
]
# 서버 요청에 대한 답. 승인 정책이 never라 오지 않아야 하지만, 오면 거절한다
SERVER_REQUEST_REPLIES = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "mcpServer/elicitation/request": {"action": "decline"},
}
CLOSED = "_closed"                       # 연결이 끊겼을 때 구독 큐에 넣는 가짜 알림


class AppServerError(RuntimeError):
    pass


class _Pending:
    """응답을 기다리는 요청 하나."""

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


class Connection:
    def __init__(self, codex: str, stderr_path: Path | None, on_exit):
        self._stderr = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr, env=os.environ.copy())
        self.pid = self.proc.pid
        self.started = time.time()
        self._on_exit = on_exit
        self._wlock = threading.Lock()           # stdin 쓰기
        self._lock = threading.Lock()            # 아래 상태
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._threads: dict[str, queue.Queue] = {}
        self.dead: str | None = None
        threading.Thread(target=self._read_loop, daemon=True, name=f"codex-app-server-{self.pid}").start()

    @property
    def alive(self) -> bool:
        return self.dead is None and self.proc.poll() is None

    # --- 보내기
    def _send(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._wlock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            self._die(f"codex app-server에 쓰지 못했다: {e}")
            raise AppServerError(self.dead or str(e)) from e

    def request(self, method: str, params: dict, timeout: float = RPC_TIMEOUT_S) -> dict:
        with self._lock:
            if self.dead:
                raise AppServerError(self.dead)
            self._next_id += 1
            rid = self._next_id
            pending = self._pending[rid] = _Pending()
        try:
            self._send({"id": rid, "method": method, "params": params})
            if not pending.event.wait(timeout):
                raise AppServerError(f"codex app-server {method} 응답이 {timeout:.0f}초 안에 오지 않았다")
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        if pending.error is not None:
            err = pending.error
            raise AppServerError(f"codex app-server {method} 실패: {err.get('message', err) if isinstance(err, dict) else err}")
        return pending.result or {}

    def request_quietly(self, method: str, params: dict, timeout: float = 10) -> None:
        """정리용 요청을 뒤에서 보낸다. 실패는 무시한다."""
        def go():
            try:
                self.request(method, params, timeout)
            except AppServerError:
                pass
        threading.Thread(target=go, daemon=True).start()

    def handshake(self) -> dict:
        """initialize 요청 → initialized 알림."""
        result = self.request("initialize", {"clientInfo": CLIENT_INFO,
                                             "capabilities": {"optOutNotificationMethods": OPT_OUT_NOTIFICATIONS}})
        self._send({"method": "initialized", "params": {}})
        return result

    # --- 스레드별 알림 구독
    def subscribe(self, thread_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            if self.dead:
                raise AppServerError(self.dead)
            self._threads[thread_id] = q
        return q

    def unsubscribe(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)

    # --- 받기
    def _read_loop(self) -> None:
        reason = "codex app-server 출력이 끊겼다"
        try:
            for raw in self.proc.stdout:
                self._dispatch_line(raw)
        except Exception as e:                    # noqa: BLE001 - 읽기 스레드는 죽지 않고 연결을 닫는다
            reason = f"codex app-server 출력을 읽지 못했다: {e}"
        try:
            reason = f"codex app-server가 종료됐다 (exit {self.proc.wait(timeout=10)})"
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._die(reason)

    def _dispatch_line(self, raw: bytes) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            log(f"codex app-server: JSON이 아닌 출력 {line[:200]!r}")
            return
        if not isinstance(msg, dict):
            return
        if "id" not in msg:
            self._route_notification(msg)
        elif "method" in msg:                            # 서버가 보낸 요청
            threading.Thread(target=self._answer_server_request, args=(msg,), daemon=True).start()
        else:                                            # 우리 요청에 대한 응답
            self._resolve(msg)

    def _resolve(self, msg: dict) -> None:
        with self._lock:
            pending = self._pending.get(msg["id"])
        if pending:
            pending.result, pending.error = msg.get("result"), msg.get("error")
            pending.event.set()

    def _route_notification(self, msg: dict) -> None:
        tid = (msg.get("params") or {}).get("threadId")
        with self._lock:
            q = self._threads.get(tid) if tid else None
        if q is not None:
            q.put(msg)

    def _answer_server_request(self, msg: dict) -> None:
        result = SERVER_REQUEST_REPLIES.get(msg["method"])
        reply = {"id": msg["id"], "result": result} if result is not None else \
            {"id": msg["id"], "error": {"code": -32601, "message": f"tiny-previz는 {msg['method']}를 지원하지 않는다"}}
        try:
            self._send(reply)
        except AppServerError:
            pass

    # --- 종료
    def _die(self, reason: str) -> None:
        with self._lock:
            if self.dead:
                return
            self.dead = reason
            pending, threads = list(self._pending.values()), list(self._threads.values())
        for p in pending:
            p.error = {"message": reason}
            p.event.set()
        for q in threads:
            q.put({"method": CLOSED, "params": {"reason": reason}})
        if self._stderr is not subprocess.DEVNULL:
            self._stderr.close()
        self._on_exit(self)

    def close(self) -> None:
        """stdin을 닫고 끝낸다. 끝나지 않으면 강제로 끝낸다."""
        if self.proc.poll() is not None:
            return
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
