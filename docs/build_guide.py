#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["markdown>=3.6"]
# ///
"""docs/guide/*.md를 독립 HTML 한 장(docs/guide/guide.html)으로 묶는다.

이미지는 base64로, Mermaid 렌더러는 vendor/mermaid.min.js를 파일 안에 넣는다.
인터넷 없이 열리고, 파일 하나만 옮겨도 그대로 보인다.
"""

from __future__ import annotations

import base64
import html
import io
import re
import tarfile
import urllib.request
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guide"
OUT = GUIDE / "guide.html"
MERMAID_VERSION = "12.3.0"
MERMAID = ROOT / "vendor" / "mermaid.min.js"

CSS = """
:root{--fg:#1f2328;--muted:#57606a;--line:#d0d7de;--bg:#ffffff;--side:#f6f8fa;--accent:#0969da}
*{box-sizing:border-box}
body{margin:0;color:var(--fg);background:var(--bg);font:16px/1.7 -apple-system,'Apple SD Gothic Neo','Noto Sans KR','Malgun Gothic',sans-serif}
nav{position:fixed;top:0;left:0;bottom:0;width:280px;overflow:auto;background:var(--side);border-right:1px solid var(--line);padding:20px 16px}
nav h1{font-size:16px;margin:0 0 4px}nav .sub{font-size:12px;color:var(--muted);margin-bottom:16px}
nav a{display:block;color:var(--fg);text-decoration:none;font-size:14px;padding:6px 8px;border-radius:6px}
nav a:hover{background:#eaeef2}nav a .n{color:var(--muted);margin-right:6px}
main{margin-left:280px;padding:32px 48px;max-width:1080px}
section.doc{border-bottom:1px solid var(--line);padding-bottom:40px;margin-bottom:40px}
h1{font-size:28px;line-height:1.3;margin:8px 0 16px}h2{font-size:22px;margin:36px 0 12px;border-bottom:1px solid var(--line);padding-bottom:6px}
h3{font-size:18px;margin:28px 0 8px}
code{background:#eff1f3;border-radius:4px;padding:1px 5px;font-size:88%}
pre{background:#f6f8fa;border:1px solid var(--line);border-radius:8px;padding:12px 14px;overflow:auto;font-size:13px;line-height:1.5}
pre code{background:none;padding:0}
pre.mermaid{background:#fff;text-align:center}
table{border-collapse:collapse;margin:12px 0;font-size:14px;display:block;overflow:auto}
th,td{border:1px solid var(--line);padding:6px 10px;vertical-align:top}th{background:#f6f8fa}
img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:8px;margin:8px 0}
blockquote{margin:12px 0;padding:4px 14px;border-left:4px solid var(--line);color:var(--muted)}
a{color:var(--accent)}
.tag{display:inline-block;font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:12px;padding:0 8px}
@media print{nav{display:none}main{margin:0}}
"""


def ensure_mermaid() -> str:
    if not MERMAID.exists():
        url = f"https://registry.npmjs.org/mermaid/-/mermaid-{MERMAID_VERSION}.tgz"
        with urllib.request.urlopen(url, timeout=120) as resp:
            archive = tarfile.open(fileobj=io.BytesIO(resp.read()), mode="r:gz")
        MERMAID.parent.mkdir(exist_ok=True)
        MERMAID.write_bytes(archive.extractfile("package/dist/mermaid.min.js").read())
    return MERMAID.read_text()


def inline_images(body: str) -> str:
    def repl(m):
        path = GUIDE / m.group(2)
        if not path.exists():
            return m.group(0)
        data = base64.b64encode(path.read_bytes()).decode()
        return f'{m.group(1)}"data:image/png;base64,{data}"'
    return re.sub(r'(src=)"(images/[^"]+)"', repl, body)


def convert(md_path: Path, anchors: dict[str, str]) -> tuple[str, str]:
    text = md_path.read_text()
    blocks: list[str] = []

    def keep_mermaid(m):  # Mermaid 원문을 Markdown 변환에서 보호한다
        blocks.append(m.group(1))
        return f"\n\nMERMAIDBLOCK{len(blocks) - 1}\n\n"
    text = re.sub(r"```mermaid\n(.*?)```", keep_mermaid, text, flags=re.S)
    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    for i, src in enumerate(blocks):
        body = body.replace(f"<p>MERMAIDBLOCK{i}</p>", f'<pre class="mermaid">{html.escape(src)}</pre>')
    for name, anchor in anchors.items():  # 문서 사이 링크를 페이지 안 앵커로
        body = body.replace(f'href="{name}"', f'href="#{anchor}"')
    title = re.search(r"^# (.+)$", md_path.read_text(), flags=re.M).group(1)
    return title, inline_images(body)


def main() -> None:
    docs = sorted(GUIDE.glob("[0-9][0-9]-*.md"))
    anchors = {d.name: d.stem for d in docs}
    nav, sections = [], []
    for d in docs:
        title, body = convert(d, anchors)
        num = d.name[:2]
        nav.append(f'<a href="#{d.stem}"><span class="n">{num}</span>{html.escape(title.split(". ", 1)[-1])}</a>')
        sections.append(f'<section class="doc" id="{d.stem}">{body}</section>')
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tiny-software-factory 단계별 소개 자료</title><style>{CSS}</style></head><body>
<nav><h1>tiny-software-factory</h1><div class="sub">단계별 소개 자료 · 문서 {len(docs)}개</div>{''.join(nav)}</nav>
<main>{''.join(sections)}</main>
<script>{ensure_mermaid()}</script>
<script>mermaid.initialize({{startOnLoad: true, securityLevel: 'strict', theme: 'neutral'}});</script>
</body></html>"""
    OUT.write_text(page)
    print(f"{OUT} ({OUT.stat().st_size / 1e6:.1f} MB, 문서 {len(docs)}개)")


if __name__ == "__main__":
    main()
