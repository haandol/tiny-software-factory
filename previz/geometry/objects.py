"""물체 피팅: 상자·원기둥 모양을 투영해 Codex가 잰 bbox(와 바닥 모서리)에 맞추는 최소제곱으로 위치·크기·yaw를 구한다."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .camera import Cam
from .colors import albedo_from_codex
from .lm import best_of
from .shapes import shape_points
from .values import NAN, clamp, points, to_float, to_hex

PRIOR = 4.0                                    # 크기 사전값의 무게 (픽셀 / log 단위)


@dataclass
class Measured:
    """물체 하나의 측정값: 모양, 화면 안으로 자른 bbox, 가장자리에 잘린 변, 바닥 모서리, 크기 사전값."""
    shape: str
    box: tuple                  # (L, T, R, B) 픽셀
    clip: dict                  # 변 이름 → 화면 가장자리에 잘렸는지
    corners: list               # 바닥 모서리 (u, v). 원기둥은 []
    prior: tuple                # Codex 크기 (가로, 깊이, 높이) m


@dataclass
class Guess:
    """피팅 초기값: 바닥 위 물체 중심과 크기."""
    center: np.ndarray
    w: float
    d: float
    h: float

    def start(self, yaw: float) -> np.ndarray:
        """피팅 변수 (x, z, log 가로, log 깊이, log 높이, yaw)."""
        return np.array([self.center[0], self.center[2], math.log(self.w), math.log(self.d), math.log(self.h), yaw])


def read_bbox(o: dict, W: int, H: int) -> tuple | None:
    """((L, T, R, B), 잘린 변). 좌표가 없거나 화면을 벗어나거나 너무 작으면 None."""
    b = o.get("bbox") or {}
    L, T = to_float(b.get("left"), NAN), to_float(b.get("top"), NAN)
    R, B = to_float(b.get("right"), NAN), to_float(b.get("bottom"), NAN)
    if not all(math.isfinite(v) for v in (L, T, R, B)):
        return None
    if max(abs(L), abs(T), abs(R), abs(B)) <= 1.5 and min(W, H) > 32:    # 0~1로 정규화된 좌표가 오면 픽셀로 바꾼다
        L, R, T, B = L * W, R * W, T * H, B * H
    L, R = sorted((clamp(L, -W, 2 * W), clamp(R, -W, 2 * W)))
    T, B = sorted((clamp(T, -H, 2 * H), clamp(B, -H, 2 * H)))
    if R - L < 2 or B - T < 2 or R < 0 or L > W or B < 0 or T > H:
        return None
    tol = 0.006 * max(W, H)
    clip = {"left": L <= tol, "right": R >= W - tol, "top": T <= tol, "bottom": B >= H - tol}
    return (max(L, 0.0), max(T, 0.0), min(R, float(W)), min(B, float(H))), clip


def size_prior(o: dict, shape: str) -> tuple[float, float, float]:
    wp = clamp(to_float(o.get("width_m"), 0.8), 0.05, 20.0)
    dp = clamp(to_float(o.get("depth_m"), wp), 0.05, 20.0)
    hp = clamp(to_float(o.get("height_m"), 0.8), 0.05, 20.0)
    if shape == "cylinder":
        wp = dp = (wp + dp) / 2
    return wp, dp, hp


def bottom_corners(o: dict, shape: str, W: int, H: int) -> list[tuple[float, float]]:
    corners = [(u, v) for u, v in points(o.get("bottom_corners")) if -W <= u <= 2 * W and -H <= v <= 2 * H]
    return [] if shape == "cylinder" else corners


def initial_guess(cam: Cam, m: Measured) -> Guess:
    """bbox 아래 가운데를 바닥으로 역투영한 점을 물체 앞쪽 끝으로 본다. 바닥이 안 보이면 크기 사전값으로 거리를 잡는다."""
    L, T, R, B = m.box
    wp, dp, _ = m.prior
    uc = (L + R) / 2
    front = None if m.clip["bottom"] else cam.ground(uc, B, max_dist=80 * cam.h)
    if front is None:
        depth = cam.f * wp / max(R - L, 2.0)
        d = cam.ray(uc, (T + B) / 2)
        p = cam.C + d * (depth / max(float(d @ cam.F), 1e-3))
        front = np.array([p[0], 0.0, p[2]])
    zc = max(float((front - cam.C) @ cam.F), 0.2)
    w0 = clamp((R - L) * zc / cam.f, 0.05, 20.0)
    dirh = np.array([front[0], 0.0, front[2]])
    dirh /= max(np.linalg.norm(dirh), 1e-6)
    center = front + dirh * w0 / 2
    h0 = clamp((B - T) * zc / cam.f, 0.05, 20.0)
    d0 = w0 * dp / wp if m.shape == "box" else w0
    return Guess(center, w0, d0, h0)


def _bbox_residuals(u: np.ndarray, v: np.ndarray, m: Measured, W: int, H: int) -> list:
    """투영한 모양의 bbox와 측정 bbox의 차. 화면 가장자리에 잘린 변은 화면 밖으로 나가기만 하면 된다."""
    L, T, R, B = m.box
    pu_l, pu_r, pv_t, pv_b = float(u.min()), float(u.max()), float(v.min()), float(v.max())
    return [max(0.0, pu_l) if m.clip["left"] else pu_l - L,
            max(0.0, W - pu_r) if m.clip["right"] else pu_r - R,
            max(0.0, pv_t) if m.clip["top"] else pv_t - T,
            max(0.0, H - pv_b) if m.clip["bottom"] else pv_b - B]


def _corner_residuals(cam: Cam, bottom: np.ndarray, corners: list) -> list:
    """측정 바닥 모서리마다 가장 가까운 투영 모서리와의 차."""
    res = []
    bu, bv, _ = cam.project(bottom)
    for (cu, cv) in corners:
        k = int(np.argmin((bu - cu) ** 2 + (bv - cv) ** 2))
        res += [0.7 * (bu[k] - cu), 0.7 * (bv[k] - cv)]
    return res


def _prior_residuals(lw: float, ld: float, lh: float, m: Measured) -> list:
    wp, dp, hp = m.prior
    res = [PRIOR * (lw - math.log(wp)), PRIOR * (lh - math.log(hp))]
    if m.shape == "box":
        res.append(PRIOR * (ld - math.log(dp)))
    return res


def residual(params, m: Measured, cam: Cam) -> np.ndarray:
    x, z, lw, ld, lh, yaw = params
    w, d, hh = math.exp(lw), math.exp(ld), math.exp(lh)
    if m.shape == "cylinder":
        d = w
    pts, bottom = shape_points(m.shape, x, z, w, d, hh, yaw)
    u, v, depth = cam.project(pts)
    behind = float(np.minimum(depth - 0.05, 0).sum())
    res = _bbox_residuals(u, v, m, cam.W, cam.H)
    if m.corners and bottom is not None:
        res += _corner_residuals(cam, bottom, m.corners)
    res += _prior_residuals(lw, ld, lh, m)
    res.append(1e3 * behind)
    return np.array(res, dtype=float)


def yaw_starts(cam: Cam, m: Measured) -> list[float]:
    """yaw 초기값 후보: 상자는 몇 각도에 더해, 가장 왼쪽 두 바닥 모서리 사이 변의 방향."""
    if m.shape == "cylinder":
        return [0.0]
    starts = [math.radians(a) for a in (0, 25, 45, 65)]
    if len(m.corners) >= 2:
        g = [cam.ground(u, v, max_dist=80 * cam.h) for u, v in m.corners]
        g = [p for p in g if p is not None]
        if len(g) >= 2:
            e = g[1] - g[0]
            starts.append(math.atan2(-e[2], e[0]))
    return starts


def final_pose(best: np.ndarray, m: Measured, guess: Guess, h: float) -> tuple:
    """피팅 변수 → (x, z, 가로, 깊이, 높이, yaw). 값이 엉뚱하면(무한, 너무 큼, 카메라 뒤) 초기값을 쓴다."""
    x, z, lw, ld, lh, yaw = (float(v) for v in best)
    w, d, hh = math.exp(lw), math.exp(ld), math.exp(lh)
    if m.shape == "cylinder":
        d, yaw = w, 0.0
    if not all(math.isfinite(v) for v in (x, z, w, d, hh)) or max(w, d, hh) > 40 * h or z > -0.05:
        x, z, w, d, hh, yaw = guess.center[0], guess.center[2], guess.w, guess.d, guess.h, 0.0
    if m.shape == "box":
        yaw = math.atan2(math.sin(yaw), math.cos(yaw))
    return x, z, w, d, hh, yaw


def fit_object(o: dict, cam: Cam) -> dict | None:
    """Codex가 잰 물체 하나에 맞는 상자·원기둥 명세. bbox를 쓸 수 없으면 None."""
    shape = "cylinder" if str(o.get("shape", "")).lower().startswith("cyl") else "box"
    bbox = read_bbox(o, cam.W, cam.H)
    if bbox is None:
        return None
    prior = size_prior(o, shape)
    m = Measured(shape, *bbox, bottom_corners(o, shape, cam.W, cam.H), prior)
    guess = initial_guess(cam, m)
    best, best_cost = best_of(lambda p: residual(p, m, cam), [guess.start(y) for y in yaw_starts(cam, m)])
    if best is None:
        best = guess.start(0.0)
    x, z, w, d, hh, yaw = final_pose(best, m, guess, cam.h)
    color = to_hex(o.get("color"), "#999999")
    return {"shape": shape, "position": [x, z], "size": [w, hh, d], "yaw": yaw,
            "color": albedo_from_codex(color), "color_codex": color,
            "fit_rms_px": math.sqrt(best_cost / 8)}
