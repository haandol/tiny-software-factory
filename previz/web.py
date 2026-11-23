"""HTTP: 요청 접수(POST /api/requests), 상태·원본 이미지, 뷰어·첫 화면 HTML, 장면 파일, three.js 사본을 내려준다."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler

from .config import KEY_RE, MAX_UPLOAD_BYTES, VENDOR, VENDOR_FILES, log, static_text
from .store import Store
from .worker import Worker

VIEWER_HTML = static_text("viewer.html")     # /view: 장면을 불러와 그리고 window.__PREVIZ__를 노출한다
INDEX_HTML = static_text("index.html")       # /: 사진을 올리고 완료되면 뷰어로 넘어간다

JSON = "application/json; charset=utf-8"
HTML = "text/html; charset=utf-8"
JS = "text/javascript; charset=utf-8"
IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"))


def image_ext(raw: bytes) -> str | None:
    """본문 앞부분으로 PNG·JPEG를 알아본다. 둘 다 아니면 None."""
    for magic, ext in IMAGE_MAGIC:
        if raw.startswith(magic):
            return ext
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tiny-previz"
    store: Store
    worker: Worker

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body, ctype: str = JSON, extra=None):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if path != "/api/requests":
            self.rfile.read(length)
            return self._send(404, {"error": "not found"})
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self.rfile.read(length)
            return self._send(400 if length <= 0 else 413, {"error": "이미지 본문이 비었거나 너무 크다"})
        raw = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = image_ext(raw)
        if ext is None:
            return self._send(415, {"error": f"PNG 또는 JPEG 이미지가 아니다 (Content-Type {ctype or '없음'})"})
        return self._accept_upload(raw, ext, ctype)

    def _accept_upload(self, raw: bytes, ext: str, ctype: str):
        key = uuid.uuid4().hex[:16]
        (self.store.dir / "uploads" / f"{key}.{ext}").write_bytes(raw)
        self.store.put(key, status="pending", error=None, created=time.time(), upload=f"{key}.{ext}",
                       content_type=ctype, bytes=len(raw))
        self.worker.submit(key)
        log(f"[{key}] 요청 접수 ({len(raw)} bytes, {ext})")
        return self._send(202, {"key": key})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, HTML)
        if path == "/view":
            return self._send(200, VIEWER_HTML, HTML)
        for pattern, route in ROUTES:
            m = re.fullmatch(pattern, path)
            if m:
                return route(self, m)
        return self._send(404, {"error": "not found"})

    def _get_request(self, m: re.Match):
        """GET /api/requests/<키>[/image]: 상태 JSON 또는 (정규화한) 원본 이미지."""
        key = m.group(1)
        rec = self.store.get(key) if KEY_RE.match(key) else None
        if rec is None:
            return self._send(404, {"status": "failed", "error": "알 수 없는 키"})
        if m.group(2):
            return self._send_input_image(key, rec)
        body = {"status": rec.get("status", "pending")}
        if rec.get("error"):
            body["error"] = rec["error"]
        return self._send(200, body)

    def _send_input_image(self, key: str, rec: dict):
        f = self.store.dir / "work" / key / "input.png"
        if not f.exists():
            f = self.store.dir / "uploads" / rec.get("upload", "")
        if not f.is_file():
            return self._send(404, {"error": "이미지 없음"})
        ctype = "image/png" if f.suffix == ".png" else "image/jpeg"
        return self._send(200, f.read_bytes(), ctype)

    def _get_scene(self, m: re.Match):
        f = self.store.dir / "scenes" / f"{m.group(1)}.js"
        if f.is_file():
            return self._send(200, f.read_bytes(), JS)
        return self._send(404, "// not found", JS)

    def _get_vendor(self, m: re.Match):
        if m.group(1) in VENDOR_FILES and (VENDOR / m.group(1)).is_file():
            return self._send(200, (VENDOR / m.group(1)).read_bytes(), JS, {"Cache-Control": "max-age=3600"})
        return self._send(404, {"error": "not found"})


ROUTES = (
    (r"/api/requests/([^/]+)(/image)?", Handler._get_request),
    (r"/scenes/([a-f0-9]{8,32})\.js", Handler._get_scene),
    (r"/vendor/([\w.]+)", Handler._get_vendor),
)


def make_handler(store: Store, worker: Worker):
    """이 저장소·작업자를 쓰는 요청 처리기 클래스 (ThreadingHTTPServer에 넘긴다)."""
    return type("Handler", (Handler,), {"store": store, "worker": worker})
