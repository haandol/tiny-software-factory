"""카메라 모델(투영·역투영)과, Codex 추정값과 지평선만으로 만든 초기 카메라."""

from __future__ import annotations

import math

import numpy as np

from .values import NAN, clamp, to_float


class Cam:
    def __init__(self, W: int, H: int, vfov_deg: float, pitch_deg: float, roll_deg: float, height: float):
        self.W, self.H, self.h = W, H, height
        self.vfov, self.pitch, self.roll = vfov_deg, pitch_deg, roll_deg
        self.f = (H / 2) / math.tan(math.radians(vfov_deg) / 2)
        self.cx, self.cy = W / 2, H / 2
        p, r = math.radians(pitch_deg), math.radians(roll_deg)
        self.F = np.array([0.0, math.sin(p), -math.cos(p)])
        r0, u0 = np.array([1.0, 0.0, 0.0]), np.array([0.0, math.cos(p), math.sin(p)])
        self.R = math.cos(r) * r0 + math.sin(r) * u0
        self.U = -math.sin(r) * r0 + math.cos(r) * u0
        self.C = np.array([0.0, height, 0.0])

    def project(self, pts: np.ndarray):
        v = pts - self.C
        zc = v @ self.F
        zs = np.where(zc > 1e-6, zc, 1e-6)
        return self.cx + self.f * (v @ self.R) / zs, self.cy - self.f * (v @ self.U) / zs, zc

    def ray(self, u: float, v: float) -> np.ndarray:
        d = self.F + ((u - self.cx) / self.f) * self.R - ((v - self.cy) / self.f) * self.U
        return d / np.linalg.norm(d)

    def ground(self, u: float, v: float, max_dist: float | None = None):
        """픽셀을 바닥(y=0)으로 역투영한다. 지평선 위면 None."""
        d = self.ray(u, v)
        if d[1] > -1e-4:
            return None
        p = self.C + d * (-self.h / d[1])
        if max_dist is not None and math.hypot(p[0], p[2]) > max_dist:
            return None
        return p


def horizon_ends(c: dict) -> tuple[float, float]:
    """Codex가 잰 지평선의 왼쪽·오른쪽 끝 y (없으면 nan)."""
    return to_float(c.get("horizon_y_at_left"), NAN), to_float(c.get("horizon_y_at_right"), NAN)


def prior_camera(a: dict, W: int, H: int, vfov: float) -> tuple[Cam, dict]:
    """주어진 화각에서 지평선(있으면)과 Codex 피치·롤을 합쳐 만든 카메라와 그 근거."""
    c = a.get("camera") or {}
    height = clamp(to_float(c.get("height_m"), 1.5), 0.3, 5.0)
    f = (H / 2) / math.tan(math.radians(vfov) / 2)
    yl, yr = horizon_ends(c)
    pitch_c = to_float(c.get("pitch_deg"), NAN)
    roll_c = to_float(c.get("roll_deg"), NAN)
    pitch_h = roll_h = NAN
    if math.isfinite(yl) and math.isfinite(yr):
        roll_h = math.degrees(math.atan2(yr - yl, W))
        # 지평선이 화면 가운데 열에서 지나는 높이 → 피치 (롤 보정 포함)
        vc = (yl + yr) / 2
        pitch_h = math.degrees(math.atan((vc - H / 2) * math.cos(math.radians(roll_h)) / f))
    pitch = _mean_finite(pitch_h, pitch_c, 0.0)
    roll = roll_h if math.isfinite(roll_h) else (roll_c if math.isfinite(roll_c) else 0.0)
    if math.isfinite(roll_h) and math.isfinite(roll_c) and roll_c * roll_h > 0:
        roll = (roll_h + roll_c) / 2           # 같은 방향이면 두 추정을 평균한다
    pitch, roll = clamp(pitch, -60.0, 45.0), clamp(roll, -30.0, 30.0)
    info = {"vfov": vfov, "pitch_from_horizon": pitch_h, "pitch_codex": pitch_c,
            "roll_from_horizon": roll_h, "roll_codex": roll_c, "pitch": pitch, "roll": roll}
    return Cam(W, H, vfov, pitch, roll, height), info


def _mean_finite(a: float, b: float, default: float) -> float:
    vals = [x for x in (a, b) if math.isfinite(x)]
    return sum(vals) / len(vals) if vals else default
