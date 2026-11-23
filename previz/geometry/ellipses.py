"""원기둥 윗면 타원으로 카메라 재기: 수평한 원의 영상(타원)은 초점거리가 주어지면 원이 놓인 평면의 법선을 정한다.

벽 선이 거의 안 보이거나(가까운 물체가 화면을 채운 사진) 정면을 보는 방에서는 벽 경계만으로 화각이 정해지지 않는다.
원기둥 윗면은 바닥과 나란한 원이므로, 사진에서 잰 그 타원(원뿔곡선 C)과 초점거리 f로 원뿔 Q = Kᵀ C K를 만들면
고윳값 분해로 원 평면의 법선이 닫힌 꼴로 나온다 (두 해 중 하나). 그 법선은 카메라 기준 연직 방향과 같아야 하므로
(화각, 피치, 롤)에 대한 관측이 된다. 원기둥의 위치·크기는 미지수로 두지 않아도 된다.

타원은 Codex bbox 위쪽(윗면이 있는 곳)에서 찾는다: 크기 격자로 흐린 사진의 기울기를 가장 많이 지나는 타원을 고르고,
그 둘레 법선 방향으로 경계점을 부분 픽셀로 잡아 매개변수 타원(중심, 반축, 기울기)을 맞추기를 반경을 줄여 가며
되풀이한다. 가까운 물체에 가린 곳과 화면 밖은 재지 않고, 윗면 앞 가장자리(아래쪽 호)가 안 보이는 타원은 쓰지 않는다.
불확실성은 경계점을 다시 뽑아 맞춘(부트스트랩) 법선의 흩어짐과 바닥값이다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .edges import EdgeImage, masked, parabolic_peak
from .lm import levenberg_marquardt

N_ARC = 120                   # 타원 둘레에서 잴 점 수
MIN_WIDTH = 36.0              # 이보다 좁은 원기둥은 타원을 재지 않는다 (픽셀)
SNAP_RADII = (4.0, 2.5, 1.5)  # 경계점을 찾는 법선 방향 반경 (픽셀)
CAUCHY = 0.8                  # 원뿔곡선 맞추기의 견고 무게 폭 (픽셀)
MIN_POINTS = 40
MIN_SPAN = 0.55               # 잡힌 경계점이 타원 가로폭의 이만큼 이상에 걸쳐야 쓴다
SIG_FLOOR_DEG = 0.6           # 법선 불확실성의 바닥값 (흐림·렌더링 계단 등)
SIG_FLOOR_PX = 0.4            # 세로 반축을 이만큼 잘못 재면 법선이 (이 값 ÷ 가로 반축) 라디안 기운다
MAX_SIG_DEG = 4.0             # 이보다 불확실한 타원은 쓰지 않는다
BOOT = 24
LOWER_ARC_MIN = 0.3           # 아래쪽 호의 경계는 위쪽 호 세기(중앙값)의 이만큼은 되어야 쓴다
MIN_LOWER = 0.25              # 경계점 가운데 아래쪽 호에 있어야 하는 비율
MIN_INLIERS = 0.7             # 잡힌 경계점 가운데 맞춘 타원 가까이 있어야 하는 비율
MAX_RMS_PX = 0.6              # 맞춘 타원에서 경계점까지 거리의 RMS 상한 (픽셀)
PAD = 4.0


@dataclass
class TopEllipse:
    conic: np.ndarray             # 3×3 원뿔곡선 (픽셀 좌표, xᵀ C x = 0)
    points: np.ndarray            # 맞춘 경계점 (K, 2)
    params: tuple                 # (uc, vc, a, b, 기울기)
    sigma: float                  # 법선 불확실성 (라디안)
    box: tuple


def ellipse_points(p: tuple, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """타원 (uc, vc, a, b, th) 위 점들과 바깥 법선."""
    uc, vc, a, b, th = p
    c, s = math.cos(th), math.sin(th)
    x, y = a * np.cos(t), b * np.sin(t)
    pts = np.stack([uc + c * x - s * y, vc + s * x + c * y], -1)
    nx, ny = np.cos(t) / max(a, 1e-6), np.sin(t) / max(b, 1e-6)
    n = np.stack([c * nx - s * ny, s * nx + c * ny], -1)
    return pts, n / np.linalg.norm(n, axis=1, keepdims=True)


def _strength(E: EdgeImage, pts: np.ndarray, n: np.ndarray) -> np.ndarray:
    """점마다 그 점의 법선 방향 기울기 세기 (채널 합)."""
    gx, gy = E.sample(pts)
    g = gx * n[..., 0:1] + gy * n[..., 1:2]
    return np.sqrt((g * g).sum(-1))


def _usable(E: EdgeImage, pts: np.ndarray, masks: list) -> np.ndarray:
    """화면 안이고 가린 사각형 밖인 점."""
    return E.inside(pts) & ~masked(pts, masks)


def plane_normals(C: np.ndarray, f: float, cx: float, cy: float) -> list[np.ndarray]:
    """원뿔곡선 C가 원의 영상일 때 그 원 평면의 법선 두 후보 (카메라 좌표: 오른쪽, 아래, 앞)."""
    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])
    Q = K.T @ C @ K
    lam, V = np.linalg.eigh(Q)
    if (lam > 0).sum() != 2:
        lam, V = np.linalg.eigh(-Q)
    if (lam > 0).sum() != 2:
        return []
    idx = np.argsort(-lam)
    l1, l2, l3 = lam[idx]
    V = V[:, idx]
    a = math.sqrt(max(l1 - l2, 0.0) / (l1 - l3))
    c = math.sqrt(max(l2 - l3, 0.0) / (l1 - l3))
    return [V @ np.array([a, 0.0, c]), V @ np.array([-a, 0.0, c])]


def up_in_camera(F: np.ndarray, R: np.ndarray, U: np.ndarray) -> np.ndarray:
    """월드 연직 방향을 카메라 좌표(오른쪽, 아래, 앞)로."""
    return np.array([R[1], -U[1], F[1]])


def normal_error(C: np.ndarray, f: float, cx: float, cy: float, up: np.ndarray) -> np.ndarray | None:
    """법선 후보 중 연직에 가까운 것과 연직의 차 (연직에 수직인 두 성분, 라디안)."""
    ns = plane_normals(C, f, cx, cy)
    if not ns:
        return None
    n = max((m if m @ up >= 0 else -m for m in ns), key=lambda m: float(m @ up))
    d = n - up * float(n @ up)
    e1 = np.cross(up, [0.0, 0.0, 1.0])
    e1 = e1 / max(np.linalg.norm(e1), 1e-9)
    e2 = np.cross(up, e1)
    return np.array([d @ e1, d @ e2])


def _free_sides(E: EdgeImage, box: tuple, masks: list) -> tuple[bool, bool]:
    """(왼쪽 변, 오른쪽 변)이 화면 가장자리에 잘렸거나 다른 물체에 가려 bbox 변이 타원 끝이 아닌지."""
    L, T, R, B = box
    out = []
    for u in (L, R):
        probe = np.array([[u, T + dv] for dv in (4.0, 10.0, 18.0)])
        seen = _usable(E, probe, masks) & (probe[:, 0] > 3) & (probe[:, 0] < E.W - 3)
        out.append(bool(not seen.all()))
    return out[0], out[1]


def _width_candidates(box: tuple, free_l: bool, free_r: bool) -> list | None:
    """(가로 반축, 중심 u) 후보: 두 변이 다 보이면 bbox 폭 둘레, 한 변만 보이면 그 변에서 넓혀 가며.
    두 변 다 잘리거나 가렸으면 None."""
    L, T, R, B = box
    a0 = (R - L) / 2
    if free_l and free_r:
        return None
    if free_l or free_r:
        return [(a, R - a if free_l else L + a) for a in a0 * np.arange(0.9, 1.81, 0.06)]
    return [(a0 + da, (L + R) / 2) for da in (-4.0, -2.0, 0.0, 2.0, 4.0)]


def _coarse(E: EdgeImage, box: tuple, masks: list, th: float = 0.0) -> tuple | None:
    """bbox 위쪽에서 기울기를 가장 많이 지나는 타원 (윗변 위치 ±6픽셀, 세로 반축 격자). 잘리거나 가린 변이 있으면
    보이는 변에서 가로 반축도 격자로 찾는다."""
    L, T, R, B = box
    widths = _width_candidates(box, *_free_sides(E, box, masks))
    if widths is None:
        return None
    t = np.linspace(0, 2 * math.pi, N_ARC, endpoint=False)
    upper = np.sin(t) < 0                                        # 위쪽 호 (실루엣)
    best, best_s = None, 0.0
    for a, uc in widths:
        for dt in np.arange(-6.0, 6.01, 1.5):
            for b in np.arange(1.5, min(0.8 * a, 0.9 * (B - T)) + 0.01, max(1.0, a / 60)):
                p = (uc, T + dt + b, a, b, th)
                pts, n = ellipse_points(p, t)
                ok = _usable(E, pts, masks)
                if (ok & upper).sum() < 0.3 * N_ARC:
                    continue
                # 아래쪽 호는 위쪽 호와 4픽셀 넘게 떨어진 곳만 센다 (얇은 타원이 실루엣 하나에 겹쳐 점수를 얻지 않게)
                lower = ok & ~upper & (2 * b * np.sin(t) >= 4.0)
                s = float(_strength(E, pts[ok & upper], n[ok & upper]).mean())
                if lower.any():
                    s += 0.5 * float(_strength(E, pts[lower], n[lower]).mean())
                if s > best_s:
                    best, best_s = p, s
    return best


def _snap_points(E: EdgeImage, p: tuple, masks: list, radius: float) -> np.ndarray:
    """타원 둘레마다 법선 방향 ±radius 안의 기울기 봉우리를 부분 픽셀로 잡는다. 위쪽 호(실루엣)는 늘 쓰고,
    아래쪽 호(윗면 앞 가장자리, 카메라가 윗면보다 낮으면 안 보인다)는 위쪽 호만큼 뚜렷한 곳만 쓴다."""
    t = np.linspace(0, 2 * math.pi, 2 * N_ARC, endpoint=False)
    pts, n = ellipse_points(p, t)
    offs = np.arange(-radius, radius + 0.01, 0.25)
    P = pts[:, None, :] + offs[None, :, None] * n[:, None, :]
    S = _strength(E, P, np.repeat(n[:, None, :], len(offs), 1))
    ok = _usable(E, P, masks).all(1)
    near = S * np.exp(-0.5 * (offs[None, :] / (0.6 * radius)) ** 2)     # 둘 다 창 안이면 예측에 가까운 봉우리
    kk = np.argmax(near, 1)
    k = np.clip(kk, 1, len(offs) - 2)
    _, s1, _, frac = parabolic_peak(S, np.arange(len(t)), k)
    off = offs[k] + 0.25 * frac
    keep = ok & (kk > 0) & (kk < len(offs) - 1) & (s1 > 0)
    upper = np.sin(t) < 0
    if (keep & upper).sum() < MIN_POINTS // 2:
        return np.zeros((0, 2))
    ref = float(np.median(s1[keep & upper]))
    keep &= np.where(upper, s1 >= 0.35 * ref, s1 >= LOWER_ARC_MIN * ref)
    return (pts + off[:, None] * n)[keep]


def ellipse_distance(p, pts: np.ndarray) -> np.ndarray:
    """점과 타원 (uc, vc, a, b, th)의 근사 거리 (Sampson, 픽셀)."""
    uc, vc, a, b, th = p
    c, s = math.cos(th), math.sin(th)
    du, dv = pts[:, 0] - uc, pts[:, 1] - vc
    x, y = c * du + s * dv, -s * du + c * dv
    F = (x / a) ** 2 + (y / b) ** 2 - 1
    g = np.sqrt((2 * x / a ** 2) ** 2 + (2 * y / b ** 2) ** 2)
    return F / np.maximum(g, 1e-9)


def fit_ellipse(pts: np.ndarray, p0: tuple, a_prior: float | None, rounds: int = 3) -> tuple | None:
    """점들에 매개변수 타원을 맞춘다 (Cauchy 재가중 LM). 가로 반축은 bbox 폭에 약하게 묶는다 (얇은 타원의 양 끝은
    점이 잘 정하지 못한다)."""
    x = np.array([p0[0], p0[1], math.log(p0[2]), math.log(max(p0[3], 0.5)), p0[4]])
    w = np.ones(len(pts))

    def fun(q):
        a, b = math.exp(min(q[2], 8.0)), math.exp(min(q[3], 8.0))
        tail = [q[4] / 0.2] + ([] if a_prior is None else [(a - a_prior) / 3.0])
        return np.r_[np.sqrt(w) * ellipse_distance((q[0], q[1], a, b, q[4]), pts), tail]
    for _ in range(rounds):
        x, _ = levenberg_marquardt(fun, x, iters=40)
        if not np.all(np.isfinite(x)):
            return None
        if max(x[2], x[3]) > 8.0:
            return None
        p = (x[0], x[1], math.exp(x[2]), math.exp(x[3]), x[4])
        w = 1.0 / (1.0 + (ellipse_distance(p, pts) / CAUCHY) ** 2)
    return (float(x[0]), float(x[1]), float(math.exp(x[2])), float(math.exp(x[3])), float(x[4]))


def param_conic(p) -> np.ndarray:
    """타원 (uc, vc, a, b, th) → 3×3 원뿔곡선 (픽셀 좌표)."""
    uc, vc, a, b, th = p
    c, s = math.cos(th), math.sin(th)
    Rm = np.array([[c, s], [-s, c]])
    M = Rm.T @ np.diag([1 / a ** 2, 1 / b ** 2]) @ Rm
    ctr = np.array([uc, vc])
    C = np.zeros((3, 3))
    C[:2, :2] = M
    C[:2, 2] = C[2, :2] = -M @ ctr
    C[2, 2] = float(ctr @ M @ ctr) - 1.0
    return C / np.linalg.norm(C)


def measure_top(E: EdgeImage, box: tuple, masks: list, f0: float, up0: np.ndarray,
                th0: float = 0.0) -> TopEllipse | None:
    """원기둥 하나의 윗면 타원. 쓸 만하지 않으면 None. f0, up0은 불확실성을 잴 때만 쓰는 대략의 카메라,
    th0은 타원 기울기의 초기값 (카메라 롤)."""
    L, T, R, B = box
    if R - L < MIN_WIDTH or T < 4 or B - T < 20:
        return None
    p = _coarse(E, box, masks, th0)
    if p is None:
        return None
    free_l, free_r = _free_sides(E, box, masks)
    a_prior = None if free_l or free_r else (R - L) / 2
    fitted = _refine(E, p, masks, a_prior, R - L)
    if fitted is None:
        return None
    p, pts = fitted
    pts = _inlier_points(p, pts)
    if pts is None:
        return None
    sigma = _bootstrap_sigma(pts, p, a_prior, f0, E.W / 2, E.H / 2, up0, SIG_FLOOR_PX / p[2])
    if sigma is None or sigma > math.radians(MAX_SIG_DEG):
        return None
    return TopEllipse(param_conic(p), pts, p, sigma, box)


def _refine(E: EdgeImage, p: tuple, masks: list, a_prior: float | None, width: float) -> tuple | None:
    """반경을 줄여 가며 경계점 잡기 → 타원 맞추기를 되풀이한 (타원, 마지막 경계점). 점이 모자라거나 타원 크기가
    bbox 폭(width)에 맞지 않으면 None."""
    a_lo, a_hi = (0.35 * width, (0.8 if a_prior else 1.2) * width)
    pts = None
    for radius in SNAP_RADII:
        pts = _snap_points(E, p, masks, radius)
        if len(pts) < MIN_POINTS:
            return None
        q = fit_ellipse(pts, p, a_prior)
        if q is None or not (a_lo < q[2] < a_hi) or q[3] < 1.0 or q[3] > 0.9 * q[2]:
            return None
        p = q
    return p, pts


def _inlier_points(p: tuple, pts: np.ndarray) -> np.ndarray | None:
    """맞춘 타원 가까이 있는 경계점. 너무 적거나 흩어졌거나, 가로로 좁거나, 아래쪽 호가 안 보이면 None."""
    d = np.abs(ellipse_distance(p, pts))
    inl = d < 3 * CAUCHY
    if inl.mean() < MIN_INLIERS or float(np.sqrt(np.mean(d[inl] ** 2))) > MAX_RMS_PX:
        return None
    pts = pts[inl]
    if len(pts) < MIN_POINTS or np.ptp(pts[:, 0]) < MIN_SPAN * 2 * p[2]:
        return None
    if _lower_fraction(p, pts) < MIN_LOWER:          # 윗면 앞 가장자리가 안 보이면 곡률 하나에 기대게 되어 쓰지 않는다
        return None
    return pts


def _lower_fraction(p: tuple, pts: np.ndarray) -> float:
    """경계점 가운데 타원 아래쪽 호(중심보다 아래, 끝부분 제외)에 있는 비율."""
    uc, vc, a, b, th = p
    c, s = math.cos(th), math.sin(th)
    du, dv = pts[:, 0] - uc, pts[:, 1] - vc
    y = -s * du + c * dv
    return float((y > 0.3 * b).mean())


def _bootstrap_sigma(pts: np.ndarray, p: tuple, a_prior: float | None, f: float, cx: float, cy: float,
                     up: np.ndarray, floor: float = 0.0) -> float | None:
    """경계점을 다시 뽑아 맞춘 법선의 흩어짐과 바닥값(SIG_FLOOR_DEG, floor 라디안 중 큰 것)을 합친 불확실성."""
    rng = np.random.default_rng(len(pts))           # 같은 점이면 같은 값 (실행마다 같은 답)
    errs = []
    for _ in range(BOOT):
        sub = pts[rng.integers(0, len(pts), len(pts))]
        q = fit_ellipse(sub, p, a_prior, rounds=1)
        e = normal_error(param_conic(q), f, cx, cy, up) if q is not None else None
        if e is None:
            return None
        errs.append(e)
    spread = float(np.sqrt(np.sum(np.var(np.array(errs), axis=0)) / 2))
    return math.hypot(spread, max(math.radians(SIG_FLOOR_DEG), floor))


def residuals(tops: list[TopEllipse], f: float, cx: float, cy: float, up: np.ndarray) -> np.ndarray:
    out = []
    for e in tops:
        d = normal_error(e.conic, f, cx, cy, up)
        out.append(np.full(2, 30.0) if d is None else d / e.sigma)
    return np.concatenate(out) if out else np.zeros(0)
