"""상자 물체 모서리의 카메라 관측 항: 상자의 수평 모서리는 서로 직각인 두 방향뿐이고 세로 모서리는 연직이다.

벽 경계가 물체에 가리거나(가까운 물체가 화면을 채운 사진) 정면을 보는 방이면 벽 경계만으로는 화각이 정해지지 않고,
화각이 표본마다 흔들리는 Codex 값(사전값)을 따라가 같은 사진에서도 요청마다 크게 달라졌다. 상자 bbox 안에서 사진의
곧은 선분(previz/geometry/lines.py)을 찾으면, 선분마다 다음 세 소실점 가운데 하나를 지나야 한다.
  연직 소실점, 수평 방향 θ의 소실점, θ + 90°의 소실점 (θ는 상자마다 모르는 방향, 잔차가 가장 작은 값으로 푼다)
연직 소실점과 두 수평 소실점이 (화각, 피치, 롤)을 정한다. 선분 잔차는 선분 중심에서 소실점으로 가는 방향과
선분 방향의 사잇각을, 그 선분의 경계점들이 그 각도를 얼마나 정확히 재는지로 나눈 값이다 (벽 경계점 잔차와 같은 눈금).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..edges import EdgeImage, Segment
from ..lines import edge_map, find_segments
from .boxes import consensus_boxes, consensus_cuboids, occluders
from .fit import line_sigma
from .model import bounded_focal_px, rotation

BOX_PAD = 6.0                 # 상자 bbox를 이만큼 넓혀 선분을 찾는다 (픽셀)
SIG_FLOOR_DEG = 0.3           # 선분 방향 오차의 바닥값 (렌더링 계단·흐림)
SEG_CAUCHY = 3.0              # 선분 잔차(σ 단위)의 견고 무게 폭: 다른 것(그림자, 뒤 물체)의 선분이 답을 끌지 못하게
DUP_PX = 4.0                  # 이미 관측으로 쓰는 경계(벽 선, 물체 옆선)와 이만큼 안에서 겹치는 선분은 빼다
DUP_DEG = 3.0
THETA_GRID = np.radians(np.arange(0.0, 90.0, 1.0))
MIN_SEGMENTS = 2              # 상자마다 선분이 이만큼은 있어야 쓴다


@dataclass
class BoxLines:
    """상자 하나의 선분들: 중심 (K, 2), 단위 방향 (K, 2), 각도 오차 (K,) 라디안."""
    mid: np.ndarray
    dir: np.ndarray
    sig: np.ndarray
    segments: list


def _segment_stats(s: Segment) -> tuple[np.ndarray, np.ndarray, float]:
    """(중심, 방향, 방향 오차 라디안). 경계점 위치 오차 σ로 맞춘 직선 방향의 오차는 σ / sqrt(Σ t²)."""
    m = s.points.mean(0)
    d = np.array([-s.normal[1], s.normal[0]])
    t = (s.points - m) @ d
    sig = line_sigma(s) / math.sqrt(max(float(t @ t), 1.0))
    return m, d, math.hypot(sig, math.radians(SIG_FLOOR_DEG))


def _duplicate(s: Segment, obs: dict) -> bool:
    """이미 관측으로 쓰는 경계와 거의 같은 선분인지 (방향이 같고 중심이 그 직선 가까이)."""
    m = s.points.mean(0)
    for o in obs.values():
        if abs(float(o.normal @ s.normal)) < math.cos(math.radians(DUP_DEG)):
            continue
        if abs(float(o.normal @ m) - o.c) > DUP_PX:
            continue
        d = np.array([-o.normal[1], o.normal[0]])
        to, ts = o.points @ d, s.points @ d
        if min(to.max(), ts.max()) - max(to.min(), ts.min()) > 0:
            return True
    return False


def box_lines(E: EdgeImage, analyses: list[dict], obs: dict | None = None) -> list[BoxLines]:
    """표본 과반이 상자라고 한 물체마다 bbox 안의 곧은 선분 (가까운 물체에 가린 곳, 이미 쓰는 경계는 뺀다)."""
    cuboids = consensus_cuboids(analyses)
    if not cuboids:
        return []
    em = edge_map(E)
    boxes = consensus_boxes(analyses)
    out = []
    for b in cuboids:
        region = (b[0] - BOX_PAD, b[1] - BOX_PAD, b[2] + BOX_PAD, b[3] + BOX_PAD)
        segs = [s for s in find_segments(E, em, region, occluders(b, boxes, E.H)) if not _duplicate(s, obs or {})]
        if len(segs) < MIN_SEGMENTS:
            continue
        stats = [_segment_stats(s) for s in segs]
        out.append(BoxLines(np.array([m for m, _, _ in stats]), np.array([d for _, d, _ in stats]),
                            np.array([g for _, _, g in stats]), segs))
    return out


def _vanishing(x, W: int, H: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """월드 x축·y축(연직)·z축 방향의 소실점 (동차 좌표, 방 yaw 0). 수평 방향 θ의 소실점은 cos θ·Vx + sin θ·Vz."""
    F, R, U = rotation(x[0], x[1], x[2], 0.0)
    f = bounded_focal_px(H, x[0])

    def vp(d):
        d = np.asarray(d, float)
        return np.array([W / 2 * (d @ F) + f * (d @ R), H / 2 * (d @ F) - f * (d @ U), d @ F])
    return vp((1, 0, 0)), vp((0, 1, 0)), vp((0, 0, 1))


def _sines(g: BoxLines, V: np.ndarray) -> np.ndarray:
    """선분마다 (중심 → 소실점 V) 방향과 선분 방향의 사잇각 sin. V는 (3,) 또는 (T, 3), 결과는 (K,) 또는 (T, K)."""
    V = np.atleast_2d(V)
    dv = V[:, None, :2] - g.mid[None] * V[:, None, 2:3]                 # (T, K, 2)
    n = np.linalg.norm(dv, axis=-1)
    cross = g.dir[None, :, 0] * dv[..., 1] - g.dir[None, :, 1] * dv[..., 0]
    dot = (g.dir[None] * dv).sum(-1)
    s = np.sign(dot + 1e-30) * cross / np.maximum(n, 1e-12)
    return s[0] if s.shape[0] == 1 else s


def _rho(r: np.ndarray) -> np.ndarray:
    return SEG_CAUCHY ** 2 * np.log1p((r / SEG_CAUCHY) ** 2)


def _box_residual(g: BoxLines, Vx: np.ndarray, Vy: np.ndarray, Vz: np.ndarray) -> np.ndarray:
    """상자 하나의 선분 잔차 (σ 단위, 부호 있음). 선분마다 세 소실점 가운데 가장 잘 맞는 것을 쓰고,
    상자 방향 θ는 견고 비용이 가장 작은 값 (1° 격자 → 포물선 보간)."""
    rv = _sines(g, Vy) / g.sig

    def at(th):
        c, s = np.cos(th)[:, None], np.sin(th)[:, None]
        r1 = _sines(g, c * Vx + s * Vz).reshape(len(th), -1) / g.sig
        r2 = _sines(g, -s * Vx + c * Vz).reshape(len(th), -1) / g.sig
        R = np.stack([np.broadcast_to(rv, r1.shape), r1, r2])        # (3, T, K)
        k = np.argmin(np.abs(R), axis=0)
        r = np.take_along_axis(R, k[None], 0)[0]
        return r, _rho(r).sum(1)
    _, cost = at(THETA_GRID)
    i = int(np.argmin(cost))
    h = float(THETA_GRID[1] - THETA_GRID[0])
    c0, c1, c2 = cost[i - 1], cost[i], cost[(i + 1) % len(cost)]
    den = c0 - 2 * c1 + c2
    frac = 0.5 * (c0 - c2) / den if den > 1e-12 else 0.0
    th = THETA_GRID[i] + h * float(np.clip(frac, -0.5, 0.5))
    r, _ = at(np.array([th]))
    return r[0]


def box_term(groups: list[BoxLines], W: int, H: int):
    """x → 상자 선분 잔차 (선분마다 Cauchy로 줄인 값)."""
    def extra(x):
        Vx, Vy, Vz = _vanishing(x, W, H)
        r = np.concatenate([_box_residual(g, Vx, Vy, Vz) for g in groups])
        return np.sign(r) * np.sqrt(_rho(r))
    return extra
