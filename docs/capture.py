#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["playwright==1.65.2", "pyyaml>=6"]
# ///
"""소개 자료용 캡처 도구. 팩토리의 일부가 아니라 docs/guide/의 이미지를 만드는 데만 쓴다.

  term   <제목> <출력 PNG> [--file 텍스트]   터미널 출력을 이미지로 (기본은 stdin)
  sheet  <평가 폴더> <출력 PNG> [--title]    평가 시트를 다시 그린다
  grid   <출력 PNG> --row "라벨|경로:캡션|경로:캡션" ...   이미지 비교표
  linear <출력 PNG> [--issue SWF-3]          Linear API로 읽은 팀 상태 또는 이슈 하나 (UI 스크린샷이 아니다)
  page   <URL> <출력 PNG> [--previz]         웹 페이지 스크린샷 (--previz: 뷰어 ready까지 기다린다)
  agent  <attempt-N.jsonl> <출력 PNG>        Claude Code 실행 기록의 도구 호출 타임라인
  run    <run.json> <출력 PNG>               실행 하나의 시도별 수용 조건·점수·비용
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tsf.env import load_dotenv  # noqa: E402
from tsf.linear import linear, linear_team, team_key  # noqa: E402
from tsf.oracle import Lab, eval_sheet_html  # noqa: E402

BASE_CSS = """
body{margin:0;background:#f4f5f7;font-family:'WenQuanYi Zen Hei',sans-serif;color:#1f2328}
.wrap{padding:20px 24px}
h1{font-size:18px;margin:0 0 12px}
.note{font-size:12px;color:#57606a;margin-top:10px}
"""


def shot(html_doc: str, out: Path, width: int = 1100) -> None:
    lab = Lab()
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        lab.screenshot_html(html_doc, out, width)
    finally:
        lab.close()
    print(out)


def doc(body: str, css: str = "") -> str:
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{BASE_CSS}{css}</style></head><body>{body}</body></html>'


def cmd_term(args):
    text = Path(args.file).read_text() if args.file else sys.stdin.read()
    css = """.term{background:#0d1117;color:#e6edf3;border-radius:8px;padding:0 0 14px;
             font-family:'WenQuanYi Zen Hei Mono',monospace;font-size:13px;line-height:1.5}
             .bar{background:#161b22;border-radius:8px 8px 0 0;padding:8px 14px;color:#8b949e;font-size:12px}
             pre{margin:0;padding:12px 16px 0;white-space:pre-wrap;word-break:break-all}
             .ok{color:#3fb950}.fail{color:#f85149}.dim{color:#8b949e}"""
    lines = []
    for line in html.escape(text).splitlines():
        s = line.lstrip()
        if s.startswith(("OK", "통과", "생성", "시험")):
            line = f'<span class="ok">{line}</span>'
        elif s.startswith(("FAIL", "실패")):
            line = f'<span class="fail">{line}</span>'
        lines.append(line)
    body = (f'<div class="wrap"><div class="term"><div class="bar">$ {html.escape(args.title)}</div>'
            f'<pre>{chr(10).join(lines)}</pre></div></div>')
    shot(doc(body, css), Path(args.out), args.width)


def cmd_sheet(args):
    d = Path(args.eval_dir)
    summary = json.loads((d / "summary.json").read_text())
    shot(eval_sheet_html(summary, d, args.title or d.name), Path(args.out), 1200)


def cmd_grid(args):
    css = """.row{display:flex;gap:12px;align-items:flex-start;background:#fff;border:1px solid #d0d7de;
             border-radius:8px;padding:10px;margin-bottom:10px}
             .label{min-width:120px;font-size:14px;font-weight:bold;padding-top:4px}
             img{width:%dpx;border-radius:4px}.cap{font-size:12px;color:#57606a;margin-top:4px}""" % args.img_width
    rows = []
    for spec in args.row:
        label, *cells = spec.split("|")
        imgs = []
        for c in cells:
            path, _, caption = c.partition(":")
            data = base64.b64encode(Path(path).read_bytes()).decode()
            imgs.append(f'<div><img src="data:image/png;base64,{data}"><div class="cap">{html.escape(caption)}</div></div>')
        rows.append(f'<div class="row"><div class="label">{html.escape(label)}</div>{"".join(imgs)}</div>')
    title = f"<h1>{html.escape(args.title)}</h1>" if args.title else ""
    note = f'<div class="note">{html.escape(args.note)}</div>' if args.note else ""
    widest = max(len(spec.split("|")) - 1 for spec in args.row)
    width = args.width or 200 + widest * (args.img_width + 12)
    shot(doc(f'<div class="wrap">{title}{"".join(rows)}{note}</div>', css), Path(args.out), width)


LINEAR_CSS = """
.board{display:flex;gap:10px;align-items:flex-start}
.col{background:#fff;border:1px solid #d0d7de;border-radius:8px;min-width:150px;flex:1}
.col h2{font-size:13px;margin:0;padding:8px 10px;border-bottom:1px solid #d0d7de;color:#57606a}
.card{margin:8px;padding:8px;border:1px solid #e1e4e8;border-radius:6px;font-size:12px;background:#fafbfc}
.id{color:#57606a;font-size:11px}
.lb{display:inline-block;border-radius:10px;padding:0 6px;font-size:10px;color:#fff;margin:3px 3px 0 0}
.chip{display:inline-block;border:1px solid #d0d7de;border-radius:10px;padding:1px 8px;margin:2px;font-size:12px;background:#fff}
.issue{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:14px}
.desc{white-space:pre-wrap;font-size:13px;background:#fafbfc;border-radius:6px;padding:8px;margin:8px 0}
.cm{border-top:1px solid #e1e4e8;padding:8px 0;font-size:12px;white-space:pre-wrap}
.cm .who{color:#57606a;font-size:11px}
"""


def cmd_linear(args):
    team = linear_team(team_key())
    note = ('<div class="note">Linear GraphQL API로 읽은 실제 데이터를 그린 그림이다. '
            'Linear 화면의 스크린샷이 아니다.</div>')
    if args.issue:
        data = linear("""query($id: String!) { issue(id: $id) {
            identifier title description state { name } labels { nodes { name color } }
            comments(first: 50) { nodes { body createdAt user { name } } } } }""", {"id": args.issue})["issue"]
        labels = "".join(f'<span class="lb" style="background:{l["color"]}">{html.escape(l["name"])}</span>'
                         for l in data["labels"]["nodes"])
        comments = "".join(
            f'<div class="cm"><div class="who">{c["createdAt"][:16].replace("T", " ")}</div>{html.escape(c["body"])}</div>'
            for c in sorted(data["comments"]["nodes"], key=lambda c: c["createdAt"]))
        body = (f'<div class="wrap"><h1>Linear 이슈 {data["identifier"]}</h1><div class="issue">'
                f'<b>{html.escape(data["title"])}</b> <span class="chip">{data["state"]["name"]}</span>{labels}'
                f'<div class="desc">{html.escape(data["description"] or "")}</div>{comments}</div>{note}</div>')
        return shot(doc(body, LINEAR_CSS), Path(args.out), args.width)

    issues = linear("""query($teamId: ID!) { issues(first: 50, filter: { team: { id: { eq: $teamId } } },
        orderBy: updatedAt) { nodes { identifier title state { name } labels { nodes { name color } } } } }""",
                      {"teamId": team["id"]})["issues"]["nodes"]
    order = ["Backlog", "Todo", "In Progress", "Needs Human", "In Review", "Done", "Canceled"]
    states = sorted({s["name"] for s in team["states"]["nodes"]}, key=lambda n: order.index(n) if n in order else 99)
    cols = []
    for st in states:
        if st == "Duplicate":
            continue
        cards = "".join(
            f'<div class="card"><div class="id">{i["identifier"]}</div>{html.escape(i["title"])}<br>'
            + "".join(f'<span class="lb" style="background:{l["color"]}">{html.escape(l["name"])}</span>'
                      for l in i["labels"]["nodes"]) + "</div>"
            for i in issues if i["state"]["name"] == st)
        cols.append(f'<div class="col"><h2>{st}</h2>{cards}</div>')
    labels = "".join(f'<span class="chip">{html.escape(l["name"])}</span>' for l in team["labels"]["nodes"])
    body = (f'<div class="wrap"><h1>Linear 팀 {team["key"]} ({html.escape(team["name"])})</h1>'
            f'<div style="margin-bottom:10px;font-size:12px">라벨: {labels}</div>'
            f'<div class="board">{"".join(cols)}</div>{note}</div>')
    shot(doc(body, LINEAR_CSS), Path(args.out), args.width)


TIMELINE_CSS = """
.box{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:12px 14px;margin-bottom:10px}
.ev{display:flex;gap:10px;font-size:12px;padding:3px 0;border-bottom:1px solid #f0f2f4}
.n{color:#8c959f;min-width:28px}.t{min-width:90px;font-weight:bold}
.d{font-family:'WenQuanYi Zen Hei Mono',monospace;white-space:pre-wrap;word-break:break-all;color:#24292f}
.txt{color:#57606a;font-style:italic}
.k{display:inline-block;margin-right:16px;font-size:13px}
.cond{font-size:12px;padding:2px 0}.ok{color:#1a7f37}.bad{color:#cf222e}
"""


def _tool_summary(name: str, inp: dict) -> str:
    if name == "Bash":
        return inp.get("command", "")[:160]
    if name in ("Write", "Edit", "Read", "MultiEdit"):
        return inp.get("file_path", "")
    if name in ("Grep", "Glob"):
        return inp.get("pattern", "")
    return json.dumps(inp, ensure_ascii=False)[:160]


def cmd_agent(args):
    """Claude Code stream-json 기록에서 에이전트가 한 일을 시간순으로 그린다."""
    events = [json.loads(l) for l in Path(args.jsonl).read_text(errors="replace").splitlines() if l.startswith("{")]
    rows, n = [], 0
    for e in events:
        if e.get("type") != "assistant":
            continue
        for c in e.get("message", {}).get("content", []):
            if c.get("type") == "tool_use":
                n += 1
                rows.append(f'<div class="ev"><span class="n">{n}</span><span class="t">{c["name"]}</span>'
                            f'<span class="d">{html.escape(_tool_summary(c["name"], c.get("input", {})))}</span></div>')
            elif c.get("type") == "text" and c.get("text", "").strip() and args.text:
                rows.append(f'<div class="ev"><span class="n"></span><span class="t">생각</span>'
                            f'<span class="d txt">{html.escape(c["text"].strip()[:220])}</span></div>')
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    init = next((e for e in events if e.get("subtype") == "init"), {})
    head = (f'<div class="box"><span class="k">모델 <b>{init.get("model")}</b></span>'
            f'<span class="k">도구 호출 <b>{n}회</b></span>'
            f'<span class="k">턴 <b>{result.get("num_turns")}</b></span>'
            f'<span class="k">시간 <b>{round((result.get("duration_ms") or 0) / 60000, 1)}분</b></span>'
            f'<span class="k">비용 <b>${result.get("total_cost_usd") or 0:.2f}</b></span></div>')
    shown = rows if len(rows) <= args.limit else rows[:args.limit // 2] + [
        f'<div class="ev"><span class="n">…</span><span class="t"></span><span class="d txt">'
        f'{len(rows) - args.limit}개 생략</span></div>'] + rows[-args.limit // 2:]
    body = f'<div class="wrap"><h1>{html.escape(args.title)}</h1>{head}<div class="box">{"".join(shown)}</div></div>'
    shot(doc(body, TIMELINE_CSS), Path(args.out), args.width)


def cmd_run(args):
    """run.json 하나를 시도별 수용 조건·점수·비용 표로 그린다."""
    rec = json.loads(Path(args.run_json).read_text())
    boxes = []
    for a in rec["attempts"]:
        conds = "".join(f'<div class="cond {"ok" if c["ok"] else "bad"}">{"통과" if c["ok"] else "실패"} '
                        f'{html.escape(c["name"])}: {html.escape(str(c["detail"]))}</div>'
                        for c in a["verdict"]["conditions"])
        sc, cl = a["score"], a["claude"]
        gen = a.get("generation_seconds") or []
        boxes.append(
            f'<div class="box"><b>시도 {a["attempt"]}</b> · 커밋 {a["commit"]} · '
            f'{"<span class=ok>eval 통과</span>" if a["verdict"]["passed"] else "<span class=bad>eval 실패</span>"}<br>'
            f'<span class="k">Claude Code {cl["seconds"]}초, ${cl["cost_usd"] or 0:.2f}, 도구 {cl["tool_calls"]}회</span>'
            f'<span class="k">공개 {sc["public"]} / 홀드아웃 {sc["holdout"]} / main {sc["main_same_seeds"]}</span>'
            f'<span class="k">Codex {a["codex_calls"]}회'
            + (f', 생성 {min(gen):.0f}~{max(gen):.0f}초' if gen else "") + f'</span>{conds}</div>')
    f = rec.get("refactor") or {}
    if f.get("ran"):
        b, a = f["code_before"], f["code_after"]
        rows = "".join(f'<div class="cond">{k}: {b[k]} → <b>{a[k]}</b></div>' for k in
                       ("code.files", "code.total_lines", "code.max_file_lines", "code.max_function_lines",
                        "code.max_complexity", "code.long_functions"))
        checks = " · ".join(f'<span class="{"ok" if v else "bad"}">{k} {"통과" if v else "실패"}</span>'
                            for k, v in (f.get("checks") or {}).items())
        sc, cl = f.get("score") or {}, f["claude"]
        boxes.append(
            f'<div class="box"><b>리팩터링 단계</b> · 커밋 {f.get("commit")} · '
            f'{"<span class=ok>수용</span>" if f.get("accepted") else "<span class=bad>거부</span>"} · {checks}<br>'
            f'<span class="k">Claude Code {cl["seconds"]}초, ${cl["cost_usd"] or 0:.2f}, 도구 {cl["tool_calls"]}회</span>'
            f'<span class="k">공개 {sc.get("public")} / 홀드아웃 {sc.get("holdout")} / main {sc.get("main_same_seeds")}</span>'
            f'<span class="k">Codex {f.get("codex_calls", 0)}회</span>{rows}</div>')
    elif f:
        boxes.append(f'<div class="box"><b>리팩터링 단계</b> · 건너뜀 ({html.escape(f.get("reason", ""))})</div>')
    d = rec.get("delivery") or {}
    tail = (f'<div class="box">결과 <b>{rec["status"]}</b> · Claude Code 합계 ${rec.get("cost_usd", 0):.2f} · '
            f'Codex {rec.get("codex_calls", 0)}회' + (f' · main {d.get("merged")} 배포 {d.get("url")}' if d.get("merged") else "")
            + "</div>")
    body = (f'<div class="wrap"><h1>{html.escape(args.title or rec["run_id"])}</h1>{"".join(boxes)}{tail}</div>')
    shot(doc(body, TIMELINE_CSS), Path(args.out), args.width)


def cmd_page(args):
    lab = Lab()
    try:
        page = lab.context.new_page()
        page.set_viewport_size({"width": args.width, "height": args.height})
        page.goto(args.url, wait_until="load")
        if args.previz:
            page.wait_for_function("() => !!window.__PREVIZ__", timeout=60_000)
            page.evaluate("() => window.__PREVIZ__.ready")
            page.wait_for_timeout(500)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=args.out, full_page=args.full)
    finally:
        lab.close()
    print(args.out)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("term"); p.add_argument("title"); p.add_argument("out")
    p.add_argument("--file"); p.add_argument("--width", type=int, default=1000)
    p = sub.add_parser("sheet"); p.add_argument("eval_dir"); p.add_argument("out"); p.add_argument("--title")
    p = sub.add_parser("grid"); p.add_argument("out"); p.add_argument("--row", action="append", required=True)
    p.add_argument("--title"); p.add_argument("--note"); p.add_argument("--width", type=int, default=0)
    p.add_argument("--img-width", type=int, default=280)
    p = sub.add_parser("linear"); p.add_argument("out"); p.add_argument("--issue"); p.add_argument("--width", type=int, default=1400)
    p = sub.add_parser("page"); p.add_argument("url"); p.add_argument("out"); p.add_argument("--previz", action="store_true")
    p.add_argument("--width", type=int, default=1100); p.add_argument("--height", type=int, default=700)
    p.add_argument("--full", action="store_true")
    p = sub.add_parser("agent"); p.add_argument("jsonl"); p.add_argument("out"); p.add_argument("--title", default="에이전트 활동")
    p.add_argument("--limit", type=int, default=60); p.add_argument("--text", action="store_true")
    p.add_argument("--width", type=int, default=1100)
    p = sub.add_parser("run"); p.add_argument("run_json"); p.add_argument("out"); p.add_argument("--title")
    p.add_argument("--width", type=int, default=1100)
    args = ap.parse_args()
    {"term": cmd_term, "sheet": cmd_sheet, "grid": cmd_grid, "linear": cmd_linear, "page": cmd_page,
     "agent": cmd_agent, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
