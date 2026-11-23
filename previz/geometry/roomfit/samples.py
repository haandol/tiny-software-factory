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


def sample_prior(analyses: list[dict], W: int, H: int) -> Prior:
    """표본 초기값 중앙값의 사전값. 하나도 못 풀면 DEFAULT_X를 중심으로 한다."""
    inits = sample_inits(analyses, W, H)
    return prior_from(np.median(np.array(inits), axis=0) if inits else DEFAULT_X)
