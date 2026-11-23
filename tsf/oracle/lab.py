"""브라우저 실험실: 헤드리스 Chromium 안의 three.js로 정답을 그리고 제품 뷰어를 측정한다.

브라우저 쪽 코드는 js/ 폴더의 실제 JS 파일이다.
  builder.js   장면 명세 → three.js 장면 (정답 렌더링과 기준 앱이 같이 쓴다)
  extract.js   계약된 장면 그래프에서 role이 붙은 메시와 카메라를 읽는다 (제품 뷰어에 주입)
  lab.js       정답 이미지 렌더링, 측정, 물체 실루엣 재렌더링
"""

from __future__ import annotations

import base64
import urllib.parse
from pathlib import Path

from ..config import VENDOR
from .policy import CHROMIUM_ARGS, IMAGE_H, IMAGE_W, VARIANT_OPTS

JS_DIR = Path(__file__).resolve().parent / "js"


def js(name: str) -> str:
    return (JS_DIR / name).read_text()


def lab_html() -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<script type="importmap">{{"imports": {{"three": "http://factory.local/three.module.js"}}}}</script>
</head><body><script type="module">
import * as THREE from 'three';
{js("builder.js")}
{js("extract.js")}
{js("lab.js")}
</script></body></html>"""


class Lab:
    """헤드리스 Chromium 하나로 정답 이미지 렌더링, 뷰어 측정, 실루엣 비교를 한다."""

    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(args=CHROMIUM_ARGS)
        self.context = self.browser.new_context(viewport={"width": IMAGE_W, "height": IMAGE_H})
        self.external_requests: list[str] = []
        page_html = lab_html()

        def guard(route):  # 로컬이 아닌 주소로 가는 요청은 막고 기록한다
            if urllib.parse.urlparse(route.request.url).hostname in ("127.0.0.1", "localhost"):
                return route.continue_()
            self.external_requests.append(route.request.url)
            return route.abort()

        def serve_factory(route):
            name = urllib.parse.urlparse(route.request.url).path.lstrip("/")
            if name == "lab.html":
                return route.fulfill(body=page_html, content_type="text/html")
            f = VENDOR / name
            if f.exists():
                return route.fulfill(body=f.read_bytes(), content_type="text/javascript")
            return route.fulfill(status=404, body="")

        self.context.route("**/*", guard)                      # 먼저 등록한 경로가 나중에 검사된다
        self.context.route("http://factory.local/**", serve_factory)
        self.page = self.context.new_page()
        self.page.goto("http://factory.local/lab.html")
        self.page.wait_for_function("() => !!window.lab", timeout=30_000)

    def render(self, fx) -> None:
        """정답 장면을 입력 이미지로 렌더링하고, 정답 측정값을 구한다."""
        url = self.page.evaluate("([s, o]) => window.lab.renderSpec(s, o)", [fx.spec, VARIANT_OPTS[fx.variant]])
        fx.png = base64.b64decode(url.split(",", 1)[1])
        fx.gt = self.page.evaluate("(s) => window.lab.analyzeSpec(s)", fx.spec)

    def measure_viewer(self, url: str, screenshot: Path) -> tuple[dict | None, int, str | None]:
        """뷰어를 열어 장면 그래프를 읽는다. (측정값, 콘솔 오류 수, 실패 이유)"""
        page = self.context.new_page()
        errors: list[str] = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            page.goto(url, wait_until="load", timeout=60_000)
            page.wait_for_function("() => !!window.__PREVIZ__", timeout=30_000)
            raw = page.evaluate("async () => {" + js("extract.js") + """
                const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('ready 시간 초과 (60초)')), 60000));
                await Promise.race([window.__PREVIZ__.ready, timeout]);
                const p = window.__PREVIZ__;
                if (!p.scene || !p.camera) throw new Error('scene 또는 camera가 없다');
                return previzExtract(p.scene, p.camera);
              }""")
            page.screenshot(path=str(screenshot))
            analysis = self.page.evaluate("([r, a]) => window.lab.analyze(r, a)", [raw, IMAGE_W / IMAGE_H])
            return analysis, len(errors), None
        except Exception as e:
            return None, len(errors), str(e).splitlines()[0][:300]
        finally:
            page.close()

    def screenshot_html(self, html: str, out: Path, width: int = 1200) -> Path:
        """HTML 한 장을 그려 PNG로 남긴다. 평가 시트와 Linear 댓글 첨부에 쓴다."""
        page = self.context.new_page()
        try:
            page.set_viewport_size({"width": width, "height": 400})
            page.set_content(html, wait_until="load")
            page.screenshot(path=str(out), full_page=True)
        finally:
            page.close()
        return out

    def close(self):
        self.browser.close()
        self._pw.stop()
