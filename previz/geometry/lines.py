"""사진에서 곧은 경계 선분 찾기: 영역(물체 bbox) 안의 경계 화소를 허프 변환으로 묶어 선분을 찾고, snap()으로 다듬는다.

상자 물체의 모서리(윗면 가장자리, 세로 모서리, 바닥 모서리)는 3D에서 수평·연직인 직선이고, 수평 모서리는 서로 직각인
두 방향뿐이다. 벽 경계가 물체에 가려도 이 선분들의 소실점이 화각·피치·롤을 정한다 (previz/geometry/roomfit/terms.py).
1) 흐린 사진의 채널별 기울기로 구조 텐서를 만들어 경계 세기와 법선 방향을 재고, 법선 방향으로 봉우리만 남긴다.
2) 영역 안에서 둘레(질감·노이즈)보다 뚜렷한 화소가 자기 법선 방향 둘레의 직선에만 표를 던진다.
3) 가장 표가 많은 직선부터, 그 직선 가까이에서 법선이 맞는 화소의 가장 긴 연속 구간을 선분으로 떼어 낸다.
4) 선분마다 snap()으로 경계점을 부분 픽셀로 다시 잡는다. 같은 사진이면 같은 선분이 나온다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .edges import EdgeImage, Segment, snap

EDGE_K = 4.0                  # 경계 세기가 사진 전체 중앙값(질감·노이즈 바닥)의 이만큼 넘는 화소만 쓴다
N_THETA = 360                 # 허프 법선 각도 칸 수 (0.5°)
VOTE_SPREAD = 4               # 화소마다 자기 법선 방향 ± 이만큼 칸에만 표를 던진다
ON_LINE_PX = 1.5              # 직선에서 이만큼 안의 화소를 그 직선의 화소로 본다
ON_LINE_DEG = 8.0             # 그 화소의 법선이 직선 법선과 이만큼 안이어야 한다
MAX_GAP_PX = 5.0              # 선분 안에서 화소가 이만큼 넘게 끊기면 다른 선분이다
MIN_LENGTH = 30.0             # 이보다 짧은 선분은 쓰지 않는다 (픽셀)
MAX_LINES = 12                # 영역마다 찾는 선분 수 상한
SNAP_RADIUS = 3.0             # 찾은 선분을 다듬을 때 둘레 반경 (픽셀)
SNAP_MIN_CONTRAST = 3.0       # 다듬은 선분이 둘레보다 이만큼 뚜렷해야 쓴다
SNAP_MIN_SUPPORT = 0.6        # 다듬은 선분의 칸 가운데 이만큼에서 경계점이 잡혀야 쓴다


@dataclass
class EdgeMap:
    """화소마다 경계 세기, 법선 각도(0~π), 법선 방향 봉우리인지, 사진 전체 세기 중앙값."""
    mag: np.ndarray
    ang: np.ndarray
    peak: np.ndarray
    noise: float


def edge_map(E: EdgeImage) -> EdgeMap:
    gx, gy = E.gx, E.gy
    jxx, jyy, jxy = (gx * gx).sum(-1), (gy * gy).sum(-1), (gx * gy).sum(-1)
    lam = (jxx + jyy) / 2 + np.sqrt(np.maximum(((jxx - jyy) / 2) ** 2 + jxy ** 2, 0.0))
    mag = np.sqrt(np.maximum(lam, 0.0))
    ang = np.mod(0.5 * np.arctan2(2 * jxy, jxx - jyy), math.pi)
    H, W = mag.shape
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    c, s = np.cos(ang), np.sin(ang)

    def at(x, y):
        x = np.clip(x, 0, W - 1.001)
        y = np.clip(y, 0, H - 1.001)
        x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
        wx, wy = x - x0, y - y0
        return ((1 - wx) * (1 - wy) * mag[y0, x0] + wx * (1 - wy) * mag[y0, x0 + 1]
                + (1 - wx) * wy * mag[y0 + 1, x0] + wx * wy * mag[y0 + 1, x0 + 1])
    peak = (mag >= at(xx + c, yy + s)) & (mag >= at(xx - c, yy - s))
    return EdgeMap(mag, ang, peak, float(np.median(mag)))


def _pixels(em: EdgeMap, region: tuple, masks) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """영역 안(가림 사각형 밖)의 뚜렷한 봉우리 화소 (x, y, 법선 각도). 화소 중심은 (j + 0.5, i + 0.5)."""
    H, W = em.mag.shape
    L, T = max(int(math.floor(region[0])), 3), max(int(math.floor(region[1])), 3)
    R, B = min(int(math.ceil(region[2])), W - 4), min(int(math.ceil(region[3])), H - 4)
    if R - L < 8 or B - T < 8:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    sub = em.mag[T:B, L:R]
    ys, xs = np.nonzero(em.peak[T:B, L:R] & (sub > EDGE_K * em.noise))
    px, py, a = xs + L + 0.5, ys + T + 0.5, em.ang[T:B, L:R][ys, xs]
    keep = np.ones(len(px), dtype=bool)
    for mL, mT, mR, mB in masks:
        keep &= ~((px >= mL) & (px <= mR) & (py >= mT) & (py <= mB))
    return px[keep], py[keep], a[keep]


def _hough_peak(px, py, a, W: int, H: int):
    """화소마다 법선 방향 둘레에 던진 표의 가장 큰 칸 (법선 각도, 원점 거리, 표 수)."""
    thetas = np.arange(N_THETA) * math.pi / N_THETA
    k0 = np.rint(a / math.pi * N_THETA).astype(int)
    rmax = math.hypot(W, H)
    ks = np.concatenate([np.mod(k0 + dk, N_THETA) for dk in range(-VOTE_SPREAD, VOTE_SPREAD + 1)])
    xs, ys = np.tile(px, 2 * VOTE_SPREAD + 1), np.tile(py, 2 * VOTE_SPREAD + 1)
    rho = xs * np.cos(thetas[ks]) + ys * np.sin(thetas[ks])
    A = np.zeros((N_THETA, int(2 * rmax) + 3))
    np.add.at(A, (ks, np.rint(rho + rmax).astype(int)), 1.0)
    A = A[:, :-2] + A[:, 1:-1] + A[:, 2:]
    kb, rb = np.unravel_index(int(np.argmax(A)), A.shape)
    return float(thetas[kb]), float(rb + 1 - rmax), float(A[kb, rb])


def _longest_run(t: np.ndarray) -> np.ndarray:
    """정렬 안 된 선 위 위치 t에서 MAX_GAP_PX 넘게 끊기지 않는 가장 긴 구간의 번호들."""
    order = np.argsort(t)
    ts = t[order]
    breaks = np.nonzero(np.diff(ts) > MAX_GAP_PX)[0]
    starts, ends = np.r_[0, breaks + 1], np.r_[breaks, len(ts) - 1]
    j = int(np.argmax(ts[ends] - ts[starts]))
    return order[starts[j]:ends[j] + 1]


def raw_segments(em: EdgeMap, region: tuple, masks=()) -> list[tuple[np.ndarray, np.ndarray]]:
    """영역 안의 선분 (끝점 둘), 표가 많은 것부터 최대 MAX_LINES개."""
    H, W = em.mag.shape
    px, py, a = _pixels(em, region, masks)
    alive = np.ones(len(px), dtype=bool)
    out = []
    for _ in range(MAX_LINES * 2):
        idx = np.nonzero(alive)[0]
        if len(idx) < MIN_LENGTH / 2 or len(out) >= MAX_LINES:
            break
        th, rho, votes = _hough_peak(px[idx], py[idx], a[idx], W, H)
        if votes < MIN_LENGTH / 2:
            break
        n = np.array([math.cos(th), math.sin(th)])
        d = np.array([-n[1], n[0]])
        dist = np.abs(px[idx] * n[0] + py[idx] * n[1] - rho)
        dang = np.abs(np.angle(np.exp(2j * (a[idx] - th)))) / 2
        near = dist <= ON_LINE_PX
        sel = idx[near & (dang <= math.radians(ON_LINE_DEG))]
        alive[idx[near]] = False
        if len(sel) < 6:
            continue
        run = sel[_longest_run(px[sel] * d[0] + py[sel] * d[1])]
        P = np.stack([px[run], py[run]], 1)
        m = P.mean(0)
        u = np.linalg.svd(P - m, full_matrices=False)[2][0]
        t = (P - m) @ u
        if t.max() - t.min() >= MIN_LENGTH:
            out.append((m + t.min() * u, m + t.max() * u))
    return out


def find_segments(E: EdgeImage, em: EdgeMap, region: tuple, masks=()) -> list[Segment]:
    """영역 안의 곧은 경계 선분을 찾아 snap()으로 다듬은 것 (뚜렷하고 충분히 잡힌 것만)."""
    out = []
    for p0, p1 in raw_segments(em, region, masks):
        s = snap(E, p0, p1, SNAP_RADIUS, list(masks), min_len=MIN_LENGTH)
        if (s is not None and s.contrast >= SNAP_MIN_CONTRAST and s.support >= SNAP_MIN_SUPPORT
                and s.length >= MIN_LENGTH):
            out.append(s)
    return out
