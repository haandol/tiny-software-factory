"""카메라 보정: Codex 추정값(사전값)과 벽 선의 평행·직교, 상자 모서리의 직각 조건으로 화각·피치·롤을 함께 푼다."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .camera import Cam, horizon_ends, prior_camera
from .lm import best_of
from .values import NAN, clamp, points, to_float

# 카메라 보정의 불확실성 (사전값) — Codex 추정값과 선 측정이 얼마나 믿을 만한지
SIG_FOV = 10.0               # Codex 화각 (도)
SIG_HORIZON_PX = 25.0        # Codex 지평선 위치 (픽셀, 화면 가운데 열)
SIG_PITCH = 3.0              # Codex 피치 (도)
SIG_ROLL = 1.5               # 지평선 기울기에서 얻은 롤 (도)
SIG_LINE = 1.0               # 벽 선 하나의 방향 (도)
SIG_BOX = 4.0                # 상자 바닥 모서리 직각 (도) — 벽 선보다 덜 정밀하다
LINE_MAX_RMS = 6.0           # 보정 후에도 선 잔차가 이보다 크면 선 측정을 버린다 (도)


def horizontal_angle(cam: Cam, p1, p2) -> float | None:
    """수평한 3D 선의 영상 위 두 점 → 그 선의 수평 방향각(라디안, x축 기준).

    두 점의 광선이 펼치는 평면 안에서 y 성분이 0인 방향이 곧 3D 선의 방향이다.
    바닥선이든 벽 윗변이든 수평선이면 같은 식으로 풀린다 (소실점이 지평선 위에 있다는 조건).
    """
    d1, d2 = cam.ray(*p1), cam.ray(*p2)
    v = d1 * d2[1] - d2 * d1[1]
    if math.hypot(v[0], v[2]) < 1e-9:
        return None
    return math.atan2(v[2], v[0])


def _wrap90(x: float) -> float:
    """각도(도)를 (-90, 90]으로 접는다 (선은 방향이 없으므로 180° 주기)."""
    return (x + 90.0) % 180.0 - 90.0


def wall_lines(a: dict) -> list:
    """벽 선 (분류, 점1, 점2). 뒷벽 선은 방의 x축("x"), 옆벽 선은 z축("z")과 나란하다. 면마다 첫 항목만 쓴다."""
    lines = []
    seen = set()
    for w in a.get("walls") or []:
        side = w.get("side") if isinstance(w, dict) else None
        if side not in ("back", "left", "right") or side in seen:
            continue
        seen.add(side)
        for key in ("bottom_edge", "top_edge"):
            pts = points(w.get(key))
            if len(pts) >= 2 and math.dist(pts[0], pts[-1]) > 20:
                lines.append(("x" if side == "back" else "z", pts[0], pts[-1]))
    return lines


def box_corner_triples(a: dict) -> list:
    """바닥 모서리 셋(왼, 앞, 오른)이 보이는 상자들의 모서리 점 목록."""
    boxes = []
    for o in a.get("objects") or []:
        if isinstance(o, dict) and str(o.get("shape")) == "box":
            pts = points(o.get("bottom_corners"))
            if len(pts) == 3 and min(math.dist(pts[0], pts[1]), math.dist(pts[1], pts[2])) > 12:
                boxes.append(pts)
    return boxes


def line_residuals(cam: Cam, lines: list, boxes: list) -> tuple[list[float], list[float]]:
    """(벽 선 방향 잔차, 상자 직각 잔차), 단위 도. 방 방향은 선들에서 닫힌 꼴로 푼다."""
    angs = []
    for cls, p1, p2 in lines:
        t = horizontal_angle(cam, p1, p2)
        if t is not None:
            angs.append(math.degrees(t) - (90.0 if cls == "z" else 0.0))
    line_res = []
    if len(angs) >= 2:
        c = sum(math.cos(math.radians(2 * x)) for x in angs)
        s = sum(math.sin(math.radians(2 * x)) for x in angs)
        psi = math.degrees(math.atan2(s, c)) / 2
        line_res = [_wrap90(x - psi) for x in angs]
    box_res = []
    for p0, p1, p2 in boxes:
        t1, t2 = horizontal_angle(cam, p1, p0), horizontal_angle(cam, p1, p2)
        if t1 is not None and t2 is not None:
            box_res.append(_wrap90(math.degrees(t1 - t2) + 90.0))
    return line_res, box_res


@dataclass
class CameraFit:
    """보정 문제: 사전값과 선 관측. x = (화각, 피치, 롤) 도 단위."""
    W: int
    H: int
    height: float
    fov: float                  # Codex 화각
    roll: float                 # 초기 카메라의 롤 (지평선 기울기·Codex 롤)
    pitch: float                # Codex 피치 (없으면 nan)
    horizon_v: float | None     # 지평선이 화면 가운데 열을 지나는 높이 (지평선이 없으면 None)
    lines: list
    boxes: list

    def cam(self, x) -> Cam:
        return Cam(self.W, self.H, float(clamp(x[0], 15.0, 120.0)), float(x[1]), float(x[2]), self.height)

    def residual(self, x) -> np.ndarray:
        cam = self.cam(x)
        r = [(x[0] - self.fov) / SIG_FOV, (x[2] - self.roll) / SIG_ROLL]
        if self.horizon_v is not None:
            vh = self.H / 2 + cam.f * math.tan(math.radians(x[1])) / math.cos(math.radians(x[2]))
            r.append((vh - self.horizon_v) / SIG_HORIZON_PX)
        if math.isfinite(self.pitch):
            r.append((x[1] - self.pitch) / SIG_PITCH)
        lr, br = line_residuals(cam, self.lines, self.boxes)
        r += [v / SIG_LINE for v in lr] + [v / SIG_BOX for v in br]
        return np.array(r, dtype=float)


def _camera_fit(a: dict, W: int, H: int, fov_c: float, cam0: Cam, info: dict) -> CameraFit:
    c = a.get("camera") or {}
    yl, yr = horizon_ends(c)
    has_h = math.isfinite(yl) and math.isfinite(yr)
    return CameraFit(W, H, cam0.h, fov_c, info["roll"], to_float(c.get("pitch_deg"), NAN),
                     (yl + yr) / 2 if has_h else None, wall_lines(a), box_corner_triples(a))


def solve_camera(a: dict, W: int, H: int) -> tuple[Cam, dict]:
    """화각·피치·롤을 함께 푼다: Codex 추정값을 사전값으로, 벽 선의 평행·직교 조건을 관측으로."""
    c = a.get("camera") or {}
    fov_c = clamp(to_float(c.get("vfov_deg"), 55.0), 15.0, 120.0)
    cam0, info = prior_camera(a, W, H, fov_c)
    fit = _camera_fit(a, W, H, fov_c, cam0, info)
    info.update({"vfov_codex": fov_c, "lines": len(fit.lines), "box_corners": len(fit.boxes)})
    if len(fit.lines) + len(fit.boxes) < 2:
        return cam0, info
    starts = [np.array([clamp(fov_c + df, 15.0, 120.0), info["pitch"], fit.roll]) for df in (0.0, -10.0, 10.0)]
    best, _ = best_of(fit.residual, starts, iters=60, accept=lambda x: 15.0 <= x[0] <= 120.0)
    if best is None:
        return cam0, info
    return _accept_if_consistent(fit, fit.cam(best), cam0, info)


def camera_fit_cost(a: dict, W: int, H: int, vfov: float, pitch: float, roll: float) -> tuple[float, int]:
    """측정값 하나가 스스로 얼마나 맞는지: solve_camera가 푼 카메라(화각·피치·롤)에서 보정 잔차(사전값·선·상자 직각)의
    제곱 평균과, 보정에 쓴 선·상자 관측 수. 같은 사진을 여러 번 잰 측정값 가운데 고를 때 쓴다 (보정 결과는 바꾸지 않는다)."""
    c = a.get("camera") or {}
    fov_c = clamp(to_float(c.get("vfov_deg"), 55.0), 15.0, 120.0)
    cam0, info = prior_camera(a, W, H, fov_c)
    fit = _camera_fit(a, W, H, fov_c, cam0, info)
    r = fit.residual(np.array([vfov, pitch, roll], dtype=float))
    return (float(np.mean(r ** 2)) if len(r) else 0.0), len(fit.lines) + len(fit.boxes)


def _accept_if_consistent(fit: CameraFit, cam: Cam, cam0: Cam, info: dict) -> tuple[Cam, dict]:
    """보정 후 선 잔차가 크면 선 측정을 버리고 초기 카메라를 쓴다."""
    lr, br = line_residuals(cam, fit.lines, fit.boxes)
    rms = math.sqrt(sum(v * v for v in lr + br) / max(len(lr + br), 1))
    info.update({"line_rms_deg": round(rms, 2), "line_residual_deg": [round(v, 2) for v in lr + br]})
    if rms > LINE_MAX_RMS:
        info["line_rejected"] = True
        return cam0, info
    info.update({"vfov": cam.vfov, "pitch": clamp(cam.pitch, -60.0, 45.0), "roll": clamp(cam.roll, -30.0, 30.0)})
    return Cam(fit.W, fit.H, cam.vfov, info["pitch"], info["roll"], fit.height), info
