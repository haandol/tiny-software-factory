"""평가 시트: 장면마다 입력 이미지, 뷰어 캡처, 지표를 한 줄로 놓은 HTML. 사람과 에이전트가 같이 본다.

안정성을 재는 장면(홀드아웃)은 두 번째 요청의 뷰어 캡처와 두 결과의 차이(stability.*)도 보인다.
"""

from __future__ import annotations

import base64
from pathlib import Path

from .policy import CODE_TARGETS, FINAL_TARGETS

CSS = (Path(__file__).resolve().parent / "sheet.css").read_text()

# (지표 키, 표시 이름, 형식). 판정 기준은 FINAL_TARGETS를 따른다.
ROWS = [
    ("camera.fov_error_deg", "화각 오차", "{:.1f}°"),
    ("camera.pitch_error_deg", "피치 오차", "{:.1f}°"),
    ("camera.roll_error_deg", "롤 오차", "{:.1f}°"),
    ("objects.f1", "물체 F1", "{:.2f}"),
    ("objects.position_error", "위치 오차", "{:.3f}"),
    ("objects.size_error", "크기 오차", "{:.2f}"),
    ("objects.color_delta_e", "색 ΔE", "{:.1f}"),
    ("reprojection.iou", "실루엣 IoU", "{:.2f}"),
    ("viewer.console_errors", "콘솔 오류", "{:.0f}"),
]
# 안정성 지표: 두 번 요청한 장면에만 있다
STABILITY_ROWS = [
    ("stability.fidelity_delta", "충실도 차 (두 요청)", "{:.1f}"),
    ("stability.fov_delta_deg", "화각 차 (두 요청)", "{:.2f}°"),
]


def _ok(key: str, value: float, targets: dict) -> bool:
    op, threshold = targets[key]
    return value <= threshold if op == "<=" else value >= threshold


def _img(path: Path, caption: str, cls: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    if path.exists():
        data = base64.b64encode(path.read_bytes()).decode()
        return f'<div{attr}><img src="data:image/png;base64,{data}"><div class="cap">{caption}</div></div>'
    return f'<div{attr}><div class="ph">없음</div><div class="cap">{caption}</div></div>'


def _cell(key: str, label: str, fmt: str, value) -> str:
    if value is None:
        return f"<tr><td>{label}</td><td>-</td></tr>"
    cls = "ok" if _ok(key, value, FINAL_TARGETS) else "bad"
    return f'<tr><td>{label}</td><td class="{cls}">{fmt.format(value)}</td></tr>'


def _metric_table(m: dict) -> str:
    cells = [_cell(key, label, fmt, m.get(key)) for key, label, fmt in ROWS]
    cells += [_cell(key, label, fmt, m[key]) for key, label, fmt in STABILITY_ROWS if key in m]
    seconds = m.get("run.seconds")
    cells.append(f"<tr><td>생성 시간</td><td>{'-' if seconds is None else f'{seconds}초'}</td></tr>")
    return "".join(cells)


def _deg(x: float | None) -> str:
    return "-" if x is None else f"{x:.1f}°"


def _repeat(r: dict, d: Path) -> str:
    """두 번째 요청: 뷰어 캡처 축소판과 그 결과의 충실도·추정 화각."""
    rep = r.get("repeat")
    if not rep:
        return ""
    caption = (f"두 번째 요청 (바이트만 다른 입력) · {rep['status']}<br>충실도 {rep['fidelity']:.1f} "
               f"(첫 요청 {r['fidelity']:.1f}) · 화각 {_deg(rep.get('fov'))} (첫 요청 {_deg(r.get('fov'))})")
    return _img(d / "viewer-repeat.png", caption, "thumb")


def _stability_line(summary: dict) -> str:
    m = summary.get("holdout", {}).get("metrics", {})
    parts = [f'<span class="{"ok" if _ok(k, m[k], FINAL_TARGETS) else "bad"}">{label.split(" (")[0]} '
             f'{fmt.format(m[k])} (기준 {FINAL_TARGETS[k][0]} {FINAL_TARGETS[k][1]:g})</span>'
             for k, label, fmt in STABILITY_ROWS if m.get(k) is not None]
    return f'<div class="sub">holdout 안정성 (같은 장면 두 번 요청): {" · ".join(parts)}</div>' if parts else ""


def _code_line(code: dict | None) -> str:
    if not code:
        return ""
    parts = []
    for key, limit in CODE_TARGETS.items():
        cls = "ok" if code.get(key, 0) <= limit else "bad"
        parts.append(f'<span class="{cls}">{key.split(".", 1)[1]} {code.get(key)} (기준 {limit})</span>')
    return f'<div class="sub">코드: 파일 {code["code.files"]}개, {code["code.total_lines"]}줄 · {" · ".join(parts)}</div>'


def eval_sheet_html(summary: dict, eval_dir: Path, title: str) -> str:
    smoke = summary["smoke"]
    sub = [f"계약 스모크 {'통과' if smoke['passed'] else '실패'}"]
    sub += [f"{v} 평균 충실도 {summary[v]['fidelity']:.1f}" for v in ("public", "holdout") if v in summary]
    rows = []
    for r in summary["fixtures"]:
        d = eval_dir / r["fixture"]
        err = f'<div class="cap bad">{r["error"]}</div>' if r.get("error") else ""
        rows.append(
            f'<div class="row">{_img(d / "input.png", "입력 이미지 (정답 장면 렌더링)")}'
            f'{_img(d / "viewer.png", "앱 뷰어 (같은 시점)")}'
            f'<div class="meta"><b>{r["fixture"]}</b> · {r["status"]}<br>충실도 <b>{r["fidelity"]:.1f}</b>'
            f'<div class="detail"><table>{_metric_table(r["metrics"])}</table>{_repeat(r, d)}</div>{err}</div></div>')
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>'
            f'<div class="wrap"><h1>{title}</h1><div class="sub">{" · ".join(sub)}</div>'
            f'{_stability_line(summary)}{_code_line(summary.get("code"))}{"".join(rows)}</div></body></html>')
