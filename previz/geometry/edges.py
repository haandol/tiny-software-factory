"""사진의 경계선 찾기: 흐린 사진의 색 기울기로, 예상 선 근처에서 실제 경계(벽과 바닥, 벽과 배경, 물체 옆선)를 찾는다.

Codex가 잰 선은 몇 픽셀씩 흔들리고, 그 몇 픽셀이 화각을 10° 넘게 바꾼다. 그래서 선 위치는 사진에서 다시 잰다.
snap()은 예상 선분 둘레(±R 픽셀)에서 기울기 세기의 합이 가장 큰 직선을 찾고(작은 허프 탐색), 그 선 가까이의
기울기 봉우리를 부분 픽셀로 잡아 경계점 목록을 돌려준다. 같은 사진이면 같은 점이 나온다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

BLUR_SIGMA = 1.4             # 질감·이미지 노이즈를 누르는 가우시안 흐림 (픽셀)
STEP = 2.0                   # 선을 따라 재는 간격 (픽셀)
BORDER = 3.0                 # 화면 가장자리에서 이만큼 안쪽만 잰다


def _gauss_kernel(sigma: float) -> np.ndarray:
    r = max(1, int(math.ceil(3 * sigma)))
    x = np.arange(-r, r + 1, dtype=np.float32)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    return k / k.sum()


def _blur(img: np.ndarray, sigma: float) -> np.ndarray:
    """(H, W, C) 분리형 가우시안 흐림 (가장자리는 복제)."""
    k = _gauss_kernel(sigma)
    r = len(k) // 2
    out = np.pad(img, ((r, r), (0, 0), (0, 0)), mode="edge")
    out = sum(k[i] * out[i:i + img.shape[0]] for i in range(len(k)))
    out = np.pad(out, ((0, 0), (r, r), (0, 0)), mode="edge")
    return sum(k[i] * out[:, i:i + img.shape[1]] for i in range(len(k)))


@dataclass
class Segment:
    """사진에서 찾은 경계 선분: 경계점(부분 픽셀), 그 점들로 맞춘 직선 (n·p = c), 지지율·대비."""
    points: np.ndarray       # (K, 2)
    normal: np.ndarray       # 단위 법선
    c: float
    support: float           # 잰 칸 가운데 경계점이 잡힌 비율
    contrast: float          # 선 위 기울기 세기 ÷ 둘레 세기

    @property
    def length(self) -> float:
        if len(self.points) < 2:
            return 0.0
        d = np.array([-self.normal[1], self.normal[0]])
        s = self.points @ d
        return float(s.max() - s.min())


class EdgeImage:
    """흐린 사진의 채널별 기울기. gradient(점, 법선) = 법선 방향 채널별 (부호 있는) 기울기."""

    def __init__(self, img: np.ndarray, sigma: float = BLUR_SIGMA):
        f = np.asarray(img, dtype=np.float32)
        if f.ndim == 2:
            f = f[..., None]
        f = f[..., :3] / 255.0
        b = _blur(f, sigma)
        self.gy, self.gx = np.gradient(b, axis=(0, 1))
        self.H, self.W = f.shape[:2]

    def inside(self, pts: np.ndarray, margin: float = BORDER) -> np.ndarray:
        return ((pts[..., 0] >= margin) & (pts[..., 0] <= self.W - margin)
                & (pts[..., 1] >= margin) & (pts[..., 1] <= self.H - margin))

    def _interp(self, pts: np.ndarray, grids: list) -> list:
        """pts (..., 2)에서 격자들(각각 (H, W, C))을 쌍선형 보간한 값 목록, 각각 (..., C). 화면 밖은 0."""
        shape = pts.shape[:-1]
        p = pts.reshape(-1, 2)
        x = np.clip(p[:, 0] - 0.5, 0, self.W - 1.001)       # 픽셀 (i, j)의 중심은 좌표 (j + 0.5, i + 0.5)
        y = np.clip(p[:, 1] - 0.5, 0, self.H - 1.001)
        x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
        wx, wy = (x - x0)[:, None], (y - y0)[:, None]
        outside = ~self.inside(p)
        out = []
        for g in grids:
            v = ((1 - wx) * (1 - wy) * g[y0, x0] + wx * (1 - wy) * g[y0, x0 + 1]
                 + (1 - wx) * wy * g[y0 + 1, x0] + wx * wy * g[y0 + 1, x0 + 1])
            v[outside] = 0.0
            out.append(v.reshape(shape + (v.shape[-1],)))
        return out

    def gradient(self, pts: np.ndarray, n: np.ndarray) -> np.ndarray:
        """pts (..., 2)에서 법선 n 방향 채널별 기울기 (..., C). 화면 밖은 0."""
        return self._interp(pts, [self.gx * n[0] + self.gy * n[1]])[0]

    def sample(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """pts (..., 2)에서 채널별 (gx, gy), 각각 (..., C). 화면 밖은 0. 점마다 법선이 다를 때 쓴다."""
        gx, gy = self._interp(pts, [self.gx, self.gy])
        return gx, gy

    def strength(self, pts: np.ndarray, n: np.ndarray) -> np.ndarray:
        g = self.gradient(pts, n)
        return np.sqrt((g * g).sum(-1))


def clip_segment(p0, p1, W: int, H: int, margin: float = BORDER):
    """선분을 화면 안(가장자리 margin 안쪽)으로 자른다 (Liang–Barsky). 화면과 안 겹치면 None."""
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    d = p1 - p0
    t0, t1 = 0.0, 1.0
    for pv, dv, lo, hi in ((p0[0], d[0], margin, W - 1 - margin), (p0[1], d[1], margin, H - 1 - margin)):
        if abs(dv) < 1e-12:
            if pv < lo or pv > hi:
                return None
            continue
        a, b = (lo - pv) / dv, (hi - pv) / dv
        a, b = min(a, b), max(a, b)
        t0, t1 = max(t0, a), min(t1, b)
        if t0 >= t1:
            return None
    return p0 + t0 * d, p0 + t1 * d


def masked(pts: np.ndarray, boxes) -> np.ndarray:
    """pts (..., 2) 가운데 사각형 (L, T, R, B) 목록 어느 하나 안에 드는 점."""
    m = np.zeros(pts.shape[:-1], dtype=bool)
    for L, T, R, B in boxes:
        m |= (pts[..., 0] >= L) & (pts[..., 0] <= R) & (pts[..., 1] >= T) & (pts[..., 1] <= B)
    return m


def parabolic_peak(S: np.ndarray, rows: np.ndarray, k: np.ndarray):
    """행마다 칸 k의 봉우리를 이웃 두 칸과 포물선으로 맞춘 (왼쪽 값, 봉우리 값, 오른쪽 값, 부분 칸 이동 ±0.5)."""
    s0, s1, s2 = S[rows, k - 1], S[rows, k], S[rows, k + 1]
    den = s0 - 2 * s1 + s2
    frac = np.where(den < -1e-12, 0.5 * (s0 - s2) / np.where(den < -1e-12, den, -1.0), 0.0)
    return s0, s1, s2, np.clip(frac, -0.5, 0.5)


WINDOW = 5                   # 경계점 하나는 앞뒤 이만큼 칸(STEP 간격)의 단면을 부호째 평균해 잡는다
OFF_STEP = 0.5               # 단면을 재는 법선 방향 간격 (픽셀)
REFINE_ROUNDS = 12           # 맞춘 직선을 따라 단면을 다시 재는 횟수 (허프 격자 직선에 끌리지 않게)
REFINE_TOL_PX = 0.02         # 다시 맞춘 직선이 양 끝에서 이만큼도 안 움직이면 멈춘다


@dataclass
class _Profiles:
    """선분을 따라 STEP 간격 칸마다 잰 법선 방향 기울기 단면."""
    t: np.ndarray            # 칸 위치 (선분 길이 비율)
    q: np.ndarray            # 칸 중심점 (칸, 2)
    n: np.ndarray            # 선분의 단위 법선
    offs: np.ndarray         # 법선 방향 오프셋 (픽셀)
    G: np.ndarray            # (칸, 오프셋, 채널) 기울기. 가린 곳·화면 밖은 0
    bad: np.ndarray          # (칸, 오프셋) 가린 곳·화면 밖
    usable: np.ndarray       # 선분 위(오프셋 0)를 잴 수 있는 칸

    @property
    def rows(self) -> np.ndarray:
        return np.arange(len(self.t))

    def index(self, o: np.ndarray) -> np.ndarray:
        """오프셋 o(픽셀)에 가장 가까운 단면 칸 번호."""
        return np.clip(np.rint((o - self.offs[0]) / OFF_STEP).astype(int), 0, len(self.offs) - 1)


def _profiles(E: EdgeImage, p0: np.ndarray, p1: np.ndarray, L: float, radius: float, fine: float,
              boxes) -> _Profiles:
    """1) 선분을 따라 칸마다 법선 방향 ±(radius + fine + 6) 픽셀의 채널별 기울기 단면."""
    t = np.arange(STEP / 2, L, STEP) / L
    q = p0[None] + t[:, None] * (p1 - p0)[None]
    d = (p1 - p0) / L
    n = np.array([-d[1], d[0]])
    offs = np.arange(-radius - fine - 6, radius + fine + 6.01, OFF_STEP)
    P = q[:, None, :] + offs[None, :, None] * n[None, None, :]
    G = E.gradient(P, n)                                       # (칸, 오프셋, 채널)
    bad = masked(P, list(boxes)) | ~E.inside(P)
    G[bad] = 0.0
    return _Profiles(t, q, n, offs, G, bad, ~bad[:, len(offs) // 2])


def _best_line(pr: _Profiles, radius: float, max_tilt: float | None) -> np.ndarray:
    """2) 작은 허프 탐색: 양 끝 이동량 (d0, d1) 격자에서 직선 위 기울기의 부호 있는 합(채널 벡터)이 가장 큰 직선.
    칸마다 그 직선의 오프셋을 돌려준다."""
    t = pr.t
    grid = np.arange(-radius, radius + 0.01, 1.0)
    D0, D1 = np.meshgrid(grid, grid, indexing="ij")
    keep = np.ones_like(D0, dtype=bool) if max_tilt is None else np.abs(D1 - D0) <= max_tilt
    D0, D1 = D0[keep], D1[keep]
    o = (1 - t)[None, :] * D0[:, None] + t[None, :] * D1[:, None]          # (후보, 칸)
    V = pr.G[pr.rows[None, :], pr.index(o)].sum(1)                          # (후보, 채널)
    score = np.sqrt((V * V).sum(-1))
    k = int(np.argmax(score))
    return (1 - t) * D0[k] + t * D1[k]


def _aligned_strength(pr: _Profiles, o_best: np.ndarray, rel: np.ndarray) -> np.ndarray:
    """3) 직선에 맞춘 단면(rel = 직선에서의 거리)을 칸마다 앞뒤 WINDOW칸 부호째 평균한 세기 (칸, rel)."""
    rows = pr.rows
    ri = pr.index(o_best[:, None] + rel[None, :])
    A = pr.G[rows[:, None], ri]                                             # (칸, rel, 채널)
    Ab = pr.bad[rows[:, None], ri]
    cs = np.cumsum(np.concatenate([np.zeros((1,) + A.shape[1:]), A]), 0)
    cn = np.cumsum(np.concatenate([np.zeros((1, A.shape[1])), (~Ab).astype(float)]), 0)
    lo, hi = np.clip(rows - WINDOW, 0, len(pr.t)), np.clip(rows + WINDOW + 1, 0, len(pr.t))
    M = (cs[hi] - cs[lo]) / np.maximum(cn[hi] - cn[lo], 1.0)[..., None]
    return np.sqrt((M * M).sum(-1))


def _edge_points(pr: _Profiles, o_best: np.ndarray, S: np.ndarray, rel: np.ndarray, fine: float):
    """칸마다 직선 ±fine 안의 봉우리를 부분 픽셀로 잡는다. (경계점 (칸, 2), 봉우리 세기, 봉우리인지, 배경 세기)."""
    rows = pr.rows
    w = int(round(fine / OFF_STEP))
    c0 = len(rel) // 2
    pk = c0 - w + np.argmax(S[:, c0 - w:c0 + w + 1], 1)
    pk = np.clip(pk, 1, len(rel) - 2)
    s0, s1, s2, frac = parabolic_peak(S, rows, pk)
    peak_off = o_best + rel[pk] + OFF_STEP * frac
    is_peak = (s1 >= s0) & (s1 >= s2) & pr.usable & (s1 > 0)
    bg = S[:, np.abs(rel) >= 4.0][pr.usable]
    bg_level = float(np.median(bg)) if bg.size else 0.0
    return pr.q + peak_off[:, None] * pr.n[None, :], s1, is_peak, bg_level


def snap(E: EdgeImage, p0, p1, radius: float, boxes: list = (), fine: float = 1.5, min_len: float = 24.0,
         max_tilt: float | None = None) -> Segment | None:
    """선분 p0–p1 둘레 ±radius 픽셀에서 실제 경계를 찾는다. boxes(가릴 사각형 목록) 안은 재지 않는다.

    경계 양쪽 면의 색 차이는 선을 따라 부호가 같고 질감 노이즈는 그렇지 않으므로, 채널별 기울기를 부호째 더한다.
    1) 선분을 따라 STEP 간격 칸마다 법선 방향 채널별 기울기 단면을 잰다.
    2) 양 끝 이동량 (d0, d1) 격자에서, 그 직선 위 기울기의 부호 있는 합(채널 벡터)의 크기가 가장 큰 직선을 고른다.
    3) 칸마다 앞뒤 WINDOW칸의 단면을 그 직선에 맞춰 부호째 평균하고, ±fine 안의 봉우리를 부분 픽셀로 잡는다.
       둘레보다 뚜렷하지 않은 봉우리와 직선에서 먼 점은 버린다.
    """
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    L = float(np.linalg.norm(p1 - p0))
    if L < min_len:
        return None
    pr = _profiles(E, p0, p1, L, radius, fine, boxes)
    if pr.usable.sum() * STEP < min_len:
        return None
    seg = _measure(pr, _best_line(pr, radius, max_tilt), fine)
    if seg is None:
        return None
    return _refine(E, seg, p0, p1, L, fine, boxes, min_len)


def _refine(E: EdgeImage, seg: Segment, p0: np.ndarray, p1: np.ndarray, L: float, fine: float, boxes,
            min_len: float) -> Segment:
    """4) 찾은 직선을 따라 단면을 다시 재어 경계점을 다시 잡는다. 허프 탐색의 직선은 1픽셀 격자 위에 있어서,
    그 직선에 맞춰 단면을 평균하면 짧은 선분의 방향이 격자 직선 쪽으로 끌린다 (같은 경계도 예상 선이 조금 다르면
    방향이 0.3°쯤 달라졌다). 맞춘 직선과 단면을 잰 직선의 중간으로 옮겨 가며, 단면을 잰 직선이 더 움직이지
    않을 때까지 되풀이한다 (끝이 휘어 두 직선 사이를 오가는 경계도 그 가운데로 모인다)."""
    n, c = seg.normal, seg.c
    for _ in range(REFINE_ROUNDS):
        q0, q1 = (p - (n @ p - c) * n for p in (p0, p1))
        pr = _profiles(E, q0, q1, L, 0.0, fine, boxes)
        if pr.usable.sum() * STEP < min_len:
            break
        new = _measure(pr, np.zeros(len(pr.t)), fine)
        if new is None:
            break
        seg = new
        moved = max(abs(float(new.normal @ q) - new.c) for q in (q0, q1))
        if moved < REFINE_TOL_PX:
            break
        n, c = _halfway(n, (q0 + q1) / 2, new)
    return seg


def _halfway(n: np.ndarray, q: np.ndarray, seg: Segment) -> tuple[np.ndarray, float]:
    """법선 n으로 점 q를 지나는 직선과 seg 직선의 중간 직선 (n, c)."""
    sn = seg.normal if seg.normal @ n >= 0 else -seg.normal
    sc = seg.c if seg.normal @ n >= 0 else -seg.c
    m = (n + sn) / np.linalg.norm(n + sn)
    mid = q + 0.5 * (sc - float(sn @ q)) * sn
    return m, float(m @ mid)


def _measure(pr: _Profiles, o_best: np.ndarray, fine: float) -> Segment | None:
    """3) 칸마다 o_best 선에 맞춘 단면에서 경계점을 잡아 직선을 맞춘 선분. 점이 모자라면 None."""
    rel = np.arange(-fine - 5, fine + 5.01, OFF_STEP)
    S = _aligned_strength(pr, o_best, rel)
    pts, s1, is_peak, bg_level = _edge_points(pr, o_best, S, rel, fine)
    if is_peak.sum() < 6:
        return None
    strong = is_peak & (s1 >= max(0.4 * float(np.median(s1[is_peak])), 2.0 * bg_level))
    pts = pts[strong]
    if len(pts) < 6:
        return None
    seg = _fit_line(pts)
    if seg is None:
        return None
    nn, c, inl = seg
    contrast = float(np.median(s1[strong][inl])) / max(bg_level, 1e-6)
    return Segment(pts[inl], nn, c, float(inl.sum()) / max(int(pr.usable.sum()), 1), contrast)


def _fit_line(pts: np.ndarray, thresh: float = 1.2, iters: int = 4):
    """점들에 직선을 맞추고 멀리 떨어진 점을 빼 가며 다시 맞춘다. (법선, c, 남긴 점 표시)."""
    inl = np.ones(len(pts), dtype=bool)
    for _ in range(iters):
        if inl.sum() < 6:
            return None
        m = pts[inl].mean(0)
        u, s, vt = np.linalg.svd(pts[inl] - m, full_matrices=False)
        n = vt[1]
        c = float(n @ m)
        r = np.abs(pts @ n - c)
        scale = max(thresh, 2.5 * 1.4826 * float(np.median(r[inl])))
        new = r <= scale
        if np.array_equal(new, inl):
            break
        inl = new
    if inl.sum() < 6:
        return None
    return n, c, inl
