"""평가 시트: 장면마다 입력 이미지, 뷰어 캡처, 지표를 한 줄로 놓은 HTML. 사람과 에이전트가 같이 본다."""

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


def _ok(key: str, value: float, targets: dict) -> bool:
    op, threshold = targets[key]
    return value <= threshold if op == "<=" else value >= threshold


def _img(path: Path, caption: str) -> str:
    if path.exists():
        data = base64.b64encode(path.read_bytes()).decode()
        return f'<div><img src="data:image/png;base64,{data}"><div class="cap">{caption}</div></div>'
    return f'<div><div class="ph">없음</div><div class="cap">{caption}</div></div>'


def _metric_table(m: dict) -> str:
    cells = []
    for key, label, fmt in ROWS:
        value = m.get(key)
        if value is None:
            cells.append(f"<tr><td>{label}</td><td>-</td></tr>")
            continue
        cls = "ok" if _ok(key, value, FINAL_TARGETS) else "bad"
        cells.append(f'<tr><td>{label}</td><td class="{cls}">{fmt.format(value)}</td></tr>')
    seconds = m.get("run.seconds")
    cells.append(f"<tr><td>생성 시간</td><td>{'-' if seconds is None else f'{seconds}초'}</td></tr>")
    return "".join(cells)


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
            f'<table>{_metric_table(r["metrics"])}</table>{err}</div></div>')
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>'
            f'<div class="wrap"><h1>{title}</h1><div class="sub">{" · ".join(sub)}</div>'
            f'{_code_line(summary.get("code"))}{"".join(rows)}</div></body></html>')
