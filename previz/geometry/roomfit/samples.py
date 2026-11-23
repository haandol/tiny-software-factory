"""Codex 표본 합치기: 표본들의 벽 선·지평선을 투표·중앙값으로 묶고, 모델 변수의 초기값·사전값을 만든다."""

from __future__ import annotations

import math

import numpy as np

from ..calibration import solve_camera
from ..camera import horizon_ends
from ..room import solve_room
from ..values import points
from .fit import Prior

SIG_FOV_PRIOR = 12.0          # 사진 경계로 화각이 정해지지 않을 때 기대는 Codex 화각(중앙값)의 폭
PRIOR_SIG = np.array([SIG_FOV_PRIOR, 15.0, 10.0, 40.0, 2.0, 2.0, 2.0, 2.0])   # 사전값 폭 (x 변수 순)
DEFAULT_X = np.array([55.0, -5.0, 0.0, 0.0, 0.7, 0.7, 1.4, 0.7])             # 표본을 하나도 못 풀 때의 사전값 중심


def codex_lines(a: dict) -> dict:
    """Codex 측정값의 벽 선 → 모서리 이름별 두 점."""
    out = {}
    for w in a.get("walls") or []:
        side = w.get("side") if isinstance(w, dict) else None
        if side not in ("back", "left", "right"):
            continue
        for key, suffix in (("bottom_edge", "bottom"), ("top_edge", "top")):
            pts = points(w.get(key))
            name = f"{side}_{suffix}"
            if len(pts) >= 2 and name not in out and math.dist(pts[0], pts[-1]) > 20:
                out[name] = (np.array(pts[0], float), np.array(pts[-1], float))
    return out


def pooled_codex_lines(analyses: list[dict]) -> dict:
    """모서리마다 표본들이 잰 Codex 선 끝점의 중앙값."""
    acc: dict[str, list] = {}
    for a in analyses:
        for k, (p0, p1) in codex_lines(a).items():
            acc.setdefault(k, []).append(np.concatenate([p0, p1]))
    out = {}
    for k, v in acc.items():
        if 2 * len(v) >= len(analyses):
            m = np.median(np.array(v), axis=0)
            out[k] = (m[:2], m[2:])
    return out


def vote_walls(analyses: list[dict]) -> tuple[frozenset, bool]:
    """표본 과반이 잰 벽과, 과반이 윗변을 잰 경우에만 윗변을 쓴다."""
    n = max(len(analyses), 1)
    counts = {"back": 0, "left": 0, "right": 0}
    tops = 0
    for a in analyses:
        lines = codex_lines(a)
        for side in counts:
            counts[side] += f"{side}_bottom" in lines
        tops += any(k.endswith("_top") for k in lines)
    walls = frozenset(s for s, c in counts.items() if 2 * c >= n)
    return walls, 2 * tops >= n


def codex_horizon(analyses: list[dict]) -> float | None:
    """표본들이 잰 지평선이 화면 가운데 열을 지나는 높이의 중앙값. 잰 표본이 없으면 None."""
    vs = []
    for a in analyses:
        yl, yr = horizon_ends(a.get("camera") or {})
        if math.isfinite(yl) and math.isfinite(yr):
            vs.append((yl + yr) / 2)
    return float(np.median(vs)) if vs else None


def initial_x(a: dict, W: int, H: int) -> np.ndarray | None:
    """Codex 측정값 하나를 지금까지의 방식(선 보정 + 바닥 역투영)으로 풀어 모델 변수로 바꾼다."""
    cam, _ = solve_camera(a, W, H)
    room = solve_room(a, cam, [])
    h = cam.h

    def lg(v, default):
        return math.log(v / h) if v > 1e-3 * h else math.log(default)
    return np.array([cam.vfov, cam.pitch, cam.roll, math.degrees(room["yaw"]),
                     lg(-room["left"], 2.0), lg(room["right"], 2.0), lg(-room["back"], 4.0), lg(room["height"], 2.0)])


def sample_inits(analyses: list[dict], W: int, H: int) -> list[np.ndarray]:
    """표본마다 푼 모델 변수 초기값 (풀리지 않거나 유한하지 않은 표본은 뺀다)."""
    inits = []
    for a in analyses:
        try:
            x0 = initial_x(a, W, H)
        except Exception:                 # noqa: BLE001 - 초기값 하나가 안 풀려도 나머지로 한다
            continue
        if np.all(np.isfinite(x0)):
            inits.append(x0)
    return inits


def prior_from(med: np.ndarray) -> Prior:
    """표본 초기값 중앙값 med를 중심으로 한 사전값. 방 yaw는 0°를 중심으로 둔다."""
    return Prior(np.array([med[0], med[1], med[2], 0.0, med[4], med[5], med[6], med[7]]), PRIOR_SIG.copy())


def _median_field(vals):
    vals = [float(v) for v in vals if isinstance(v, (int, float)) and math.isfinite(v)]
    return float(np.median(vals)) if vals else None


def pooled_analysis(analyses: list[dict]) -> dict:
    """표본들을 합친 측정값 하나: 카메라·방 값은 중앙값, 벽 선은 모서리별 끝점 중앙값 (과반이 잰 것만)."""
    cam = {k: _median_field([(a.get("camera") or {}).get(k) for a in analyses])
           for k in ("vfov_deg", "pitch_deg", "roll_deg", "horizon_y_at_left", "horizon_y_at_right", "height_m")}
    room = {k: _median_field([(a.get("room") or {}).get(k) for a in analyses]) for k in ("width_m", "depth_m", "height_m")}
    walls = {}
    for k, (p0, p1) in pooled_codex_lines(analyses).items():
        side, edge = k.split("_")
        w = walls.setdefault(side, {"side": side, "bottom_edge": [], "top_edge": []})
        w[f"{edge}_edge"] = [{"x": float(p0[0]), "y": float(p0[1])}, {"x": float(p1[0]), "y": float(p1[1])}]
    from ...merge import merged_measurements_all
    return {"camera": {k: v for k, v in cam.items() if v is not None}, "room": {k: v for k, v in room.items() if v is not None},
            "walls": list(walls.values()), "objects": merged_measurements_all(analyses)}


import os
PRIOR_MODE = os.environ.get("PREVIZ_PRIOR_MODE", "median")


def prior_center(analyses: list[dict], W: int, H: int) -> np.ndarray | None:
    inits = sample_inits(analyses, W, H)
    if not inits:
        return None
    med = np.median(np.array(inits), axis=0)
    if PRIOR_MODE == "pooled":
        try:
            x = initial_x(pooled_analysis(analyses), W, H)
            if np.all(np.isfinite(x)):
                return x
        except Exception:
            pass
    if PRIOR_MODE == "mix":
        try:
            x = initial_x(pooled_analysis(analyses), W, H)
            if np.all(np.isfinite(x)):
                return np.median(np.array(inits + [x]), axis=0)
        except Exception:
            pass
    return med


def sample_prior(analyses: list[dict], W: int, H: int) -> Prior:
    """표본 초기값 중앙값의 사전값. 하나도 못 풀면 DEFAULT_X를 중심으로 한다."""
    med = prior_center(analyses, W, H)
    return prior_from(med if med is not None else DEFAULT_X)
