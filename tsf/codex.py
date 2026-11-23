"""Codex app-server 클라이언트: 표준 입출력 JSON-RPC로 initialize → thread/start → turn/start를 보낸다.

doctor가 제품과 같은 방식으로 Codex를 점검할 때 쓴다. 제품은 자기 클라이언트를 따로 가진다.
"""

from __future__ import annotations

import json
import subprocess
import time


class AppServer:
    def __init__(self, env: dict[str, str]):
        self.proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=env)
        self.next_id = 0
        self.request("initialize", {"clientInfo": {"name": "tiny-software-factory", "version": "1"}})
        self._send({"method": "initialized", "params": {}})

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _read(self, timeout: float) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server가 종료됐다")
            if line.strip():
                return json.loads(line)
        raise RuntimeError("codex app-server 응답 시간 초과")

    def request(self, method: str, params: dict, timeout: float = 120) -> dict:
        self.next_id += 1
        rid = self.next_id
        self._send({"id": rid, "method": method, "params": params})
        while True:
            msg = self._read(timeout)
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 실패: {msg['error']}")
                return msg["result"]

    def run_turn(self, thread_id: str, items: list[dict], timeout: float = 300) -> tuple[str, dict | None, float]:
        """turn을 하나 실행하고 (마지막 답, 토큰 사용량, 걸린 초)를 돌려준다."""
        started = time.time()
        self.request("turn/start", {"threadId": thread_id, "input": items})
        answer, usage = "", None
        while True:
            msg = self._read(timeout)
            method, params = msg.get("method"), msg.get("params", {})
            if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                answer = params["item"].get("text", "")
            elif method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage", {}).get("total")
            elif method == "turn/completed":
                return answer, usage, round(time.time() - started, 1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
