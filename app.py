#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26", "pillow>=10"]
# ///
"""tiny previz — 사진 한 장을 같은 구도의 three.js 프리비즈 장면으로 바꾸는 단일 파일 서비스.

흐름
  POST /api/requests (이미지 바이트) → 키 발급(202) → 백그라운드 작업
    1) Codex를 헤드리스로 호출해(`codex exec -i <이미지> --output-schema`) 이미지 측정값을 JSON으로 받는다
       (화각, 지평선, 벽과 바닥의 경계선, 물체마다 bbox·바닥 모서리·대표 색, 주광 방향).
    2) 측정값을 기하로 풀어 3D 장면을 만든다
       (지평선 → 피치·롤, 바닥 역투영 → 벽, 물체 모양을 투영해 bbox에 맞추는 최소제곱 피팅).
    3) <data-dir>/scenes/<키>.js 에 `export function build(THREE)` ES 모듈로 저장한다.
  GET /api/requests/<키>  → {"status": "pending" | "done" | "failed", "error"?}
  GET /view?key=<키>      → 로컬 three.js 사본으로 장면을 그리는 뷰어 (window.__PREVIZ__)

실행: uv run app.py --port <port> --data-dir <dir>
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor"
VENDOR_FILES = {"three.module.js", "three.core.js"}

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MAX_IMAGE_SIDE = 1280                     # 큰 이미지는 이 크기로 줄여 Codex에 넘긴다 (기하는 줄인 좌표계로 푼다)
CODEX_TIMEOUT_S = int(os.environ.get("PREVIZ_CODEX_TIMEOUT", "420"))
CODEX_TRIES = int(os.environ.get("PREVIZ_CODEX_TRIES", "2"))
CONCURRENCY = int(os.environ.get("PREVIZ_CONCURRENCY", "8"))
KEY_RE = re.compile(r"^[a-f0-9]{8,32}$")
HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def log(*parts) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


# ---------------------------------------------------------------------------
# 1. Codex: 이미지 측정
# ---------------------------------------------------------------------------

def _num(desc: str) -> dict:
    return {"type": "number", "description": desc}


_PT = {"type": "object", "additionalProperties": False, "required": ["x", "y"],
       "properties": {"x": {"type": "number"}, "y": {"type": "number"}}}

ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["camera", "room", "walls", "objects", "sun"],
    "properties": {
        "camera": {
            "type": "object", "additionalProperties": False,
            "required": ["vfov_deg", "pitch_deg", "roll_deg", "horizon_y_at_left", "horizon_y_at_right", "height_m"],
            "properties": {
                "vfov_deg": _num("vertical field of view in degrees"),
                "pitch_deg": _num("camera pitch in degrees, negative = looking down"),
                "roll_deg": _num("camera roll in degrees, positive = horizon lower on the right side"),
                "horizon_y_at_left": _num("pixel y of the horizon line at x = 0"),
                "horizon_y_at_right": _num("pixel y of the horizon line at x = image width"),
                "height_m": _num("camera height above the floor in meters"),
            },
        },
        "room": {
            "type": "object", "additionalProperties": False,
            "required": ["floor_color", "wall_color", "background_color", "width_m", "depth_m", "height_m"],
            "properties": {
                "floor_color": {"type": "string"}, "wall_color": {"type": "string"},
                "background_color": {"type": "string"},
                "width_m": {"type": "number"}, "depth_m": {"type": "number"}, "height_m": {"type": "number"},
            },
        },
        "walls": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["side", "bottom_edge", "top_edge"],
                "properties": {
                    "side": {"type": "string", "enum": ["back", "left", "right"]},
                    "bottom_edge": {"type": "array", "items": _PT},
                    "top_edge": {"type": "array", "items": _PT},
                },
            },
        },
        "objects": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["shape", "color", "bbox", "bottom_corners", "width_m", "depth_m", "height_m"],
                "properties": {
                    "shape": {"type": "string", "enum": ["box", "cylinder"]},
                    "color": {"type": "string"},
                    "bbox": {"type": "object", "additionalProperties": False,
                             "required": ["left", "top", "right", "bottom"],
                             "properties": {k: {"type": "number"} for k in ("left", "top", "right", "bottom")}},
                    "bottom_corners": {"type": "array", "items": _PT},
                    "width_m": {"type": "number"}, "depth_m": {"type": "number"}, "height_m": {"type": "number"},
                },
            },
        },
        "sun": {
            "type": "object", "additionalProperties": False, "required": ["x", "y", "z"],
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
        },
    },
}

PROMPT = """\
You are the camera-matching step of a 3D previsualization tool. Look carefully at the attached \
image ({W}x{H} pixels). It shows an interior: a floor, walls (a back wall facing the camera and \
possibly left/right side walls; above the walls there may be plain background instead of a ceiling), \
and a few main solid objects standing on the floor. Each main object can be approximated by one \
box or one upright cylinder.

Report measurements as JSON following the output schema. Pixel coordinates: x from the left edge \
(0..{W}), y from the top edge (0..{H}). Values may lie outside the image when you extrapolate. \
Be precise: the 3D scene is reconstructed from your pixel coordinates by exact geometry.

camera
- vfov_deg: vertical field of view in degrees. Judge it from perspective: how strongly the \
floor/wall edges that run away from the camera converge, how quickly things shrink with distance, \
how foreshortened the floor and the tops of objects are. Narrow lens ~30-45, normal ~50-60, wide ~65-80.
- horizon_y_at_left, horizon_y_at_right: y of the horizon (eye-level line = vanishing line of the \
floor; edges between the floor and the side walls, and the top edges of the side walls, converge on \
it) at x=0 and at x={W}. They differ when the camera is rolled.
- pitch_deg: negative when looking down (horizon above the image center), positive when looking up.
- roll_deg: positive when the horizon is lower on the right side.
- height_m: camera height above the floor in meters (typically 1.0-1.8).

room: floor_color, wall_color, background_color (the area above the walls) as #rrggbb, and rough \
room size in meters (width_m left-right, depth_m front-back, height_m wall height).

walls: one entry per visible wall (side = back, left or right).
- bottom_edge: two points on the line where that wall meets the floor, far apart. Extrapolate \
the line behind objects or beyond the image border if necessary.
- top_edge: two points on the top edge of that wall if it is visible, otherwise [].

objects: every main object standing on the floor, nearest first. Ignore shadows, floor/wall \
texture, image noise and tiny items. Never list walls, floor or shadows as objects.
- shape: "box" or "cylinder" (upright).
- color: the base color of the object as #rrggbb (its lit side, not the shaded side).
- bbox: tight pixel bounding box of the whole object (left, top, right, bottom). If part of it is \
hidden behind another object, estimate the full extent. If it is cut off by the image border, use the border.
- bottom_corners: for boxes, the image points where each visible vertical edge meets the floor, \
left to right (usually 2 or 3 points). For cylinders: [].
- width_m, depth_m, height_m: estimated real size in meters, consistent with the camera height.

sun: unit vector pointing from the scene toward the main light, in camera-aligned axes \
(x = right, y = up, z = toward the camera). Infer it from shading and shadows.
"""


def run_codex(image_path: Path, work: Path, W: int, H: int) -> dict:
    """Codex를 헤드리스로 한 번 호출해 측정값 JSON을 받는다. 환경 변수는 그대로 넘긴다."""
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI를 찾을 수 없다")
    schema_path = work / "schema.json"
    schema_path.write_text(json.dumps(ANALYSIS_SCHEMA))
    out_path = work / "codex-last-message.json"
    out_path.unlink(missing_ok=True)
    cmd = [codex, "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
           "-s", "read-only", "-C", str(work), "--output-schema", str(schema_path),
           "-o", str(out_path), "-i", str(image_path)]          # 프롬프트는 stdin으로 넘긴다
    started = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=os.environ.copy(), cwd=str(work))
    RUNNING.add(proc)
    try:
        stdout, stderr = proc.communicate(PROMPT.format(W=W, H=H), timeout=CODEX_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"codex exec가 {CODEX_TIMEOUT_S}초 안에 끝나지 않았다")
    finally:
        RUNNING.discard(proc)
    (work / "codex.log").write_text(f"$ {' '.join(cmd)}\n--- stdout\n{stdout}\n--- stderr\n{stderr}\n")
    if proc.returncode != 0:
        raise RuntimeError(f"codex exec 실패 (exit {proc.returncode}): {(stderr or stdout).strip()[-300:]}")
    text = out_path.read_text() if out_path.exists() else stdout
    data = _parse_json(text)
    log(f"codex 완료 {image_path.parent.name} {time.time() - started:.0f}초")
    return data


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    raise RuntimeError(f"Codex 응답이 JSON이 아니다: {text.strip()[:200]!r}")


class _Running:
    """실행 중인 Codex 프로세스. 앱이 종료될 때 함께 정리한다."""

    def __init__(self):
        self._procs: set = set()
        self._lock = threading.Lock()

    def add(self, p):
        with self._lock:
            self._procs.add(p)

    def discard(self, p):
        with self._lock:
            self._procs.discard(p)

    def kill_all(self):
        with self._lock:
            for p in list(self._procs):
                try:
                    p.kill()
                except OSError:
                    pass


RUNNING = _Running()


# ---------------------------------------------------------------------------
# 2. 기하: 측정값 → 3D 장면 명세
#
# 월드 좌표: y 위, 바닥 y=0, 카메라는 (0, h, 0)에서 수평 방향 -z를 본다 (방이 카메라 기준으로 놓인다).
# 카메라 자세는 three.js와 같다: lookAt(피치) 다음 rotateZ(롤).
# ---------------------------------------------------------------------------

def _f(v, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _hex(v, default: str) -> str:
    m = HEX_RE.match(str(v or "").strip())
    return "#" + m.group(1).lower() if m else default


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


# 카메라 보정의 불확실성 (사전값) — Codex 추정값과 선 측정이 얼마나 믿을 만한지
SIG_FOV = 10.0               # Codex 화각 (도)
SIG_HORIZON_PX = 25.0        # Codex 지평선 위치 (픽셀, 화면 가운데 열)
SIG_PITCH = 3.0              # Codex 피치 (도)
SIG_ROLL = 1.5               # 지평선 기울기에서 얻은 롤 (도)
SIG_LINE = 1.0               # 벽 선 하나의 방향 (도)
SIG_BOX = 4.0                # 상자 바닥 모서리 직각 (도) — 벽 선보다 덜 정밀하다
LINE_MAX_RMS = 6.0           # 보정 후에도 선 잔차가 이보다 크면 선 측정을 버린다 (도)


def _pt(p) -> tuple[float, float] | None:
    if not isinstance(p, dict):
        return None
    u, v = _f(p.get("x"), float("nan")), _f(p.get("y"), float("nan"))
    return (u, v) if math.isfinite(u) and math.isfinite(v) else None


def horizontal_angle(cam: "Cam", p1, p2) -> float | None:
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


def camera_observations(a: dict) -> tuple[list, list]:
    """벽 선 (분류, 점1, 점2)과 상자 바닥 모서리 (왼, 앞, 오른) 목록."""
    lines, boxes = [], []
    seen = set()
    for w in a.get("walls") or []:
        side = w.get("side") if isinstance(w, dict) else None
        if side not in ("back", "left", "right") or side in seen:
            continue
        seen.add(side)
        for key in ("bottom_edge", "top_edge"):
            pts = [p for p in (_pt(q) for q in (w.get(key) or [])) if p]
            if len(pts) >= 2 and math.dist(pts[0], pts[-1]) > 20:
                lines.append(("x" if side == "back" else "z", pts[0], pts[-1]))
    for o in a.get("objects") or []:
        if isinstance(o, dict) and str(o.get("shape")) == "box":
            pts = [p for p in (_pt(q) for q in (o.get("bottom_corners") or [])) if p]
            if len(pts) == 3 and min(math.dist(pts[0], pts[1]), math.dist(pts[1], pts[2])) > 12:
                boxes.append(pts)
    return lines, boxes


def line_residuals(cam: "Cam", lines: list, boxes: list) -> tuple[list[float], list[float]]:
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


def solve_camera(a: dict, W: int, H: int) -> tuple["Cam", dict]:
    """화각·피치·롤을 함께 푼다: Codex 추정값을 사전값으로, 벽 선의 평행·직교 조건을 관측으로."""
    c = a.get("camera") or {}
    fov_c = _clamp(_f(c.get("vfov_deg"), 55.0), 15.0, 120.0)
    cam0, info = _camera_at(a, W, H, fov_c)
    height = cam0.h
    yl, yr = _f(c.get("horizon_y_at_left"), float("nan")), _f(c.get("horizon_y_at_right"), float("nan"))
    has_h = math.isfinite(yl) and math.isfinite(yr)
    vc = (yl + yr) / 2 if has_h else float("nan")
    pitch_c = _f(c.get("pitch_deg"), float("nan"))
    roll_p = info["roll"]
    lines, boxes = camera_observations(a)
    info.update({"vfov_codex": fov_c, "lines": len(lines), "box_corners": len(boxes)})
    if len(lines) + len(boxes) < 2 and not (len(lines) >= 2):
        return cam0, info

    def cam_of(x):
        return Cam(W, H, float(_clamp(x[0], 15.0, 120.0)), float(x[1]), float(x[2]), height)

    def residual(x):
        cam = cam_of(x)
        r = [(x[0] - fov_c) / SIG_FOV, (x[2] - roll_p) / SIG_ROLL]
        if has_h:   # 지평선이 화면 가운데 열을 지나는 높이
            vh = H / 2 + cam.f * math.tan(math.radians(x[1])) / math.cos(math.radians(x[2]))
            r.append((vh - vc) / SIG_HORIZON_PX)
        if math.isfinite(pitch_c):
            r.append((x[1] - pitch_c) / SIG_PITCH)
        lr, br = line_residuals(cam, lines, boxes)
        r += [v / SIG_LINE for v in lr] + [v / SIG_BOX for v in br]
        return np.array(r, dtype=float)

    best, best_cost = None, float("inf")
    for df in (0.0, -10.0, 10.0):
        x0 = np.array([_clamp(fov_c + df, 15.0, 120.0), info["pitch"], roll_p])
        try:
            x, cost = _lm(residual, x0, iters=60)
        except (ValueError, FloatingPointError, OverflowError):
            continue
        if cost < best_cost and 15.0 <= x[0] <= 120.0:
            best, best_cost = x, cost
    if best is None:
        return cam0, info
    cam = cam_of(best)
    lr, br = line_residuals(cam, lines, boxes)
    rms = math.sqrt(sum(v * v for v in lr + br) / max(len(lr + br), 1))
    info.update({"line_rms_deg": round(rms, 2), "line_residual_deg": [round(v, 2) for v in lr + br]})
    if rms > LINE_MAX_RMS:
        info["line_rejected"] = True
        return cam0, info
    info.update({"vfov": cam.vfov, "pitch": _clamp(cam.pitch, -60.0, 45.0), "roll": _clamp(cam.roll, -30.0, 30.0)})
    return Cam(W, H, cam.vfov, info["pitch"], info["roll"], height), info


def _camera_at(a: dict, W: int, H: int, vfov: float) -> tuple[Cam, dict]:
    c = a.get("camera") or {}
    height = _clamp(_f(c.get("height_m"), 1.5), 0.3, 5.0)
    f = (H / 2) / math.tan(math.radians(vfov) / 2)
    yl, yr = _f(c.get("horizon_y_at_left"), float("nan")), _f(c.get("horizon_y_at_right"), float("nan"))
    pitch_c = _f(c.get("pitch_deg"), float("nan"))
    roll_c = _f(c.get("roll_deg"), float("nan"))
    pitch_h = roll_h = float("nan")
    if math.isfinite(yl) and math.isfinite(yr):
        roll_h = math.degrees(math.atan2(yr - yl, W))
        # 지평선이 화면 가운데 열에서 지나는 높이 → 피치 (롤 보정 포함)
        vc = (yl + yr) / 2
        pitch_h = math.degrees(math.atan((vc - H / 2) * math.cos(math.radians(roll_h)) / f))
    pitch = _combine(pitch_h, pitch_c, 0.0)
    roll = roll_h if math.isfinite(roll_h) else (roll_c if math.isfinite(roll_c) else 0.0)
    if math.isfinite(roll_h) and math.isfinite(roll_c) and roll_c * roll_h > 0:
        roll = (roll_h + roll_c) / 2           # 같은 방향이면 두 추정을 평균한다
    pitch, roll = _clamp(pitch, -60.0, 45.0), _clamp(roll, -30.0, 30.0)
    info = {"vfov": vfov, "pitch_from_horizon": pitch_h, "pitch_codex": pitch_c,
            "roll_from_horizon": roll_h, "roll_codex": roll_c, "pitch": pitch, "roll": roll}
    return Cam(W, H, vfov, pitch, roll, height), info


def _combine(a: float, b: float, default: float) -> float:
    vals = [x for x in (a, b) if math.isfinite(x)]
    return sum(vals) / len(vals) if vals else default


# --- 물체: 모양을 투영해 측정 bbox(와 바닥 모서리)에 맞춘다 -----------------------

_CIRCLE = np.linspace(0, 2 * math.pi, 64, endpoint=False)


def shape_points(shape: str, x: float, z: float, w: float, d: float, h: float, yaw: float):
    """(전체 점, 바닥 모서리 점). yaw는 three.js rotation.y와 같은 뜻(라디안)."""
    if shape == "cylinder":
        r = w / 2
        cx, cz = x + r * np.cos(_CIRCLE), z + r * np.sin(_CIRCLE)
        bottom = np.stack([cx, np.zeros_like(cx), cz], 1)
        top = bottom + np.array([0.0, h, 0.0])
        return np.concatenate([bottom, top]), None
    lx = np.array([-w / 2, w / 2, w / 2, -w / 2])
    lz = np.array([-d / 2, -d / 2, d / 2, d / 2])
    cs, sn = math.cos(yaw), math.sin(yaw)
    wx, wz = x + lx * cs + lz * sn, z - lx * sn + lz * cs
    bottom = np.stack([wx, np.zeros(4), wz], 1)
    top = bottom + np.array([0.0, h, 0.0])
    return np.concatenate([bottom, top]), bottom


def _lm(fun, x0: np.ndarray, iters: int = 80):
    """작은 Levenberg–Marquardt (수치 야코비안)."""
    x = x0.astype(float).copy()
    r = fun(x)
    cost = float(r @ r)
    lam = 1e-2
    for _ in range(iters):
        J = np.empty((r.size, x.size))
        for j in range(x.size):
            e = 1e-5 * max(1.0, abs(x[j]))
            xp = x.copy()
            xp[j] += e
            J[:, j] = (fun(xp) - r) / e
        A, g = J.T @ J, J.T @ r
        improved = False
        for _ in range(12):
            try:
                dx = np.linalg.solve(A + lam * (np.diag(np.diag(A)) + 1e-9 * np.eye(x.size)), -g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            r2 = fun(x + dx)
            c2 = float(r2 @ r2)
            if c2 < cost:
                x, r, cost = x + dx, r2, c2
                lam = max(lam / 3, 1e-9)
                improved = True
                break
            lam *= 5
        if not improved or float(np.abs(dx).max()) < 1e-7:
            break
    return x, cost


def fit_object(o: dict, cam: Cam) -> dict | None:
    W, H, h = cam.W, cam.H, cam.h
    shape = "cylinder" if str(o.get("shape", "")).lower().startswith("cyl") else "box"
    b = o.get("bbox") or {}
    L, T = _f(b.get("left"), float("nan")), _f(b.get("top"), float("nan"))
    R, B = _f(b.get("right"), float("nan")), _f(b.get("bottom"), float("nan"))
    if not all(math.isfinite(v) for v in (L, T, R, B)):
        return None
    if max(abs(L), abs(T), abs(R), abs(B)) <= 1.5 and min(W, H) > 32:    # 0~1로 정규화된 좌표가 오면 픽셀로 바꾼다
        L, R, T, B = L * W, R * W, T * H, B * H
    L, R = sorted((_clamp(L, -W, 2 * W), _clamp(R, -W, 2 * W)))
    T, B = sorted((_clamp(T, -H, 2 * H), _clamp(B, -H, 2 * H)))
    if R - L < 2 or B - T < 2 or R < 0 or L > W or B < 0 or T > H:
        return None
    tol = 0.006 * max(W, H)
    clip = {"left": L <= tol, "right": R >= W - tol, "top": T <= tol, "bottom": B >= H - tol}
    L, R, T, B = max(L, 0.0), min(R, float(W)), max(T, 0.0), min(B, float(H))

    wp = _clamp(_f(o.get("width_m"), 0.8), 0.05, 20.0)
    dp = _clamp(_f(o.get("depth_m"), wp), 0.05, 20.0)
    hp = _clamp(_f(o.get("height_m"), 0.8), 0.05, 20.0)
    if shape == "cylinder":
        wp = dp = (wp + dp) / 2
    corners = []
    for p in o.get("bottom_corners") or []:
        if isinstance(p, dict):
            u, v = _f(p.get("x"), float("nan")), _f(p.get("y"), float("nan"))
            if math.isfinite(u) and math.isfinite(v) and -W <= u <= 2 * W and -H <= v <= 2 * H:
                corners.append((u, v))
    if shape == "cylinder":
        corners = []

    # 초기값: bbox 아래 가운데를 바닥으로 역투영한 점을 물체 앞쪽 끝으로 본다
    uc = (L + R) / 2
    front = None if clip["bottom"] else cam.ground(uc, B, max_dist=80 * h)
    if front is None:
        depth = cam.f * wp / max(R - L, 2.0)
        d = cam.ray(uc, (T + B) / 2)
        p = cam.C + d * (depth / max(float(d @ cam.F), 1e-3))
        front = np.array([p[0], 0.0, p[2]])
    zc = max(float((front - cam.C) @ cam.F), 0.2)
    w0 = _clamp((R - L) * zc / cam.f, 0.05, 20.0)
    dirh = np.array([front[0], 0.0, front[2]])
    dirh /= max(np.linalg.norm(dirh), 1e-6)
    center = front + dirh * w0 / 2
    h0 = _clamp((B - T) * zc / cam.f, 0.05, 20.0)
    d0 = w0 * dp / wp if shape == "box" else w0

    PRIOR = 4.0                                    # 크기 사전값의 무게 (픽셀 / log 단위)

    def residual(params):
        x, z, lw, ld, lh, yaw = params
        w, d, hh = math.exp(lw), math.exp(ld), math.exp(lh)
        if shape == "cylinder":
            d = w
        pts, bottom = shape_points(shape, x, z, w, d, hh, yaw)
        u, v, depth = cam.project(pts)
        res = []
        behind = float(np.minimum(depth - 0.05, 0).sum())
        pu_l, pu_r, pv_t, pv_b = float(u.min()), float(u.max()), float(v.min()), float(v.max())
        res.append(max(0.0, pu_l) if clip["left"] else pu_l - L)
        res.append(max(0.0, W - pu_r) if clip["right"] else pu_r - R)
        res.append(max(0.0, pv_t) if clip["top"] else pv_t - T)
        res.append(max(0.0, H - pv_b) if clip["bottom"] else pv_b - B)
        if corners and bottom is not None:
            bu, bv, _ = cam.project(bottom)
            for (cu, cv) in corners:
                k = int(np.argmin((bu - cu) ** 2 + (bv - cv) ** 2))
                res += [0.7 * (bu[k] - cu), 0.7 * (bv[k] - cv)]
        res += [PRIOR * (lw - math.log(wp)), PRIOR * (lh - math.log(hp))]
        if shape == "box":
            res.append(PRIOR * (ld - math.log(dp)))
        res.append(1e3 * behind)
        return np.array(res, dtype=float)

    starts = [0.0] if shape == "cylinder" else [math.radians(a) for a in (0, 25, 45, 65)]
    if shape == "box" and len(corners) >= 2:
        g = [cam.ground(u, v, max_dist=80 * h) for u, v in corners]
        g = [p for p in g if p is not None]
        if len(g) >= 2:
            # 가장 왼쪽 두 모서리 사이 변의 방향을 yaw 후보로 쓴다
            e = g[1] - g[0]
            starts.append(math.atan2(-e[2], e[0]))
    best, best_cost = None, float("inf")
    for yaw0 in starts:
        x0 = np.array([center[0], center[2], math.log(w0), math.log(d0), math.log(h0), yaw0])
        try:
            x, cost = _lm(residual, x0)
        except (FloatingPointError, ValueError, OverflowError):
            continue
        if cost < best_cost:
            best, best_cost = x, cost
    if best is None:
        best = np.array([center[0], center[2], math.log(w0), math.log(d0), math.log(h0), 0.0])
    x, z, lw, ld, lh, yaw = (float(v) for v in best)
    w, d, hh = math.exp(lw), math.exp(ld), math.exp(lh)
    if shape == "cylinder":
        d, yaw = w, 0.0
    if not all(math.isfinite(v) for v in (x, z, w, d, hh)) or max(w, d, hh) > 40 * h or z > -0.05:
        x, z, w, d, hh, yaw = center[0], center[2], w0, d0, h0, 0.0
    if shape == "box":
        yaw = math.atan2(math.sin(yaw), math.cos(yaw))
    return {"shape": shape, "position": [x, z], "size": [w, hh, d], "yaw": yaw,
            "color": albedo_from_codex(_hex(o.get("color"), "#999999")), "color_codex": _hex(o.get("color"), "#999999"),
            "fit_rms_px": math.sqrt(best_cost / 8)}


# Codex가 말하는 물체 색에는 음영이 남아 있어 실제 재질 색보다 어둡다 (공개·홀드아웃 26개 물체에서
# 선형 밝기 1.2배가 ΔE를 9.8 → 7.4로 줄였다). 장면 전체에 같은 보정을 한 번 곱한다.
CODEX_COLOR_GAIN = 1.2


def albedo_from_codex(hex_color: str) -> str:
    rgb = np.array([int(hex_color[i:i + 2], 16) for i in (1, 3, 5)], float)
    return _lin_to_hex(_srgb_to_lin(rgb) * CODEX_COLOR_GAIN)


# --- 방: 벽과 바닥의 경계선을 바닥으로 역투영한다 ---------------------------------

def solve_room(a: dict, cam: Cam, objects: list[dict]) -> dict:
    h = cam.h
    room = a.get("room") or {}
    width_m = _clamp(_f(room.get("width_m"), 5.0), 1.5, 40.0)
    depth_m = _clamp(_f(room.get("depth_m"), 6.0), 1.5, 60.0)
    height_m = _clamp(_f(room.get("height_m"), 2.8), 1.0, 20.0)

    lines: dict[str, list[np.ndarray]] = {}
    tops: dict[str, list[tuple[float, float]]] = {}
    for wall in a.get("walls") or []:
        side = str(wall.get("side", ""))
        if side not in ("back", "left", "right") or side in lines:
            continue
        pts = []
        for p in wall.get("bottom_edge") or []:
            if isinstance(p, dict):
                g = cam.ground(_f(p.get("x"), float("nan")), _f(p.get("y"), float("nan")), max_dist=60 * h) \
                    if math.isfinite(_f(p.get("x"), float("nan"))) and math.isfinite(_f(p.get("y"), float("nan"))) else None
                if g is not None:
                    pts.append(g)
        if len(pts) >= 2 and np.linalg.norm(pts[-1] - pts[0]) > 0.05 * h:
            lines[side] = [pts[0], pts[-1]]
        tops[side] = [(_f(p.get("x"), float("nan")), _f(p.get("y"), float("nan")))
                      for p in (wall.get("top_edge") or []) if isinstance(p, dict)]

    # 방의 방향(yaw): 뒷벽은 로컬 x축, 옆벽은 로컬 z축과 나란하다
    yaws = []
    if "back" in lines:
        e = lines["back"][1] - lines["back"][0]
        if e[0] < 0:
            e = -e
        yaws.append(math.atan2(-e[2], e[0]))
    for side in ("left", "right"):
        if side in lines:
            e = lines[side][1] - lines[side][0]
            if e[2] < 0:
                e = -e
            yaws.append(math.atan2(e[0], e[2]))
    yaws = [y for y in yaws if abs(y) < math.radians(50)]
    psi = float(np.median(yaws)) if yaws else 0.0
    cs, sn = math.cos(psi), math.sin(psi)

    def local(p):  # 월드 → 방 로컬 (x, z)
        return p[0] * cs - p[2] * sn, p[0] * sn + p[2] * cs

    back = left = right = None
    if "back" in lines:
        zs = [local(p)[1] for p in lines["back"]]
        if np.mean(zs) < -0.3 * h:
            back = float(np.mean(zs))
    if "left" in lines:
        xs = [local(p)[0] for p in lines["left"]]
        if np.mean(xs) < -0.1 * h:
            left = float(np.mean(xs))
    if "right" in lines:
        xs = [local(p)[0] for p in lines["right"]]
        if np.mean(xs) > 0.1 * h:
            right = float(np.mean(xs))
    obj_local = [local([o["position"][0], 0.0, o["position"][1]]) for o in objects]
    obj_r = [max(o["size"][0], o["size"][2]) for o in objects]
    if back is None:
        far = min([lz - r for (_, lz), r in zip(obj_local, obj_r)] + [-0.6 * depth_m])
        back = far - 0.5
    if left is None and right is None:
        left, right = -width_m / 2, width_m / 2
    elif left is None:
        left = min(right - width_m, -0.3 * h)
    elif right is None:
        right = max(left + width_m, 0.3 * h)
    # 물체가 벽 밖으로 나가지 않게 넓힌다
    for (lx, lz), r in zip(obj_local, obj_r):
        left, right, back = min(left, lx - r), max(right, lx + r), min(back, lz - r)
    front = max(1.0 * h, 0.4)

    # 벽 높이: 윗변의 점을 벽 평면과 만나게 해 높이를 구한다
    heights = []
    for side, pts in tops.items():
        for (u, v) in pts:
            if not (math.isfinite(u) and math.isfinite(v)):
                continue
            d = cam.ray(u, v)
            dl = np.array([d[0] * cs - d[2] * sn, d[1], d[0] * sn + d[2] * cs])
            if side == "back" and dl[2] < -1e-3:
                t = back / dl[2]
            elif side == "left" and dl[0] < -1e-3:
                t = left / dl[0]
            elif side == "right" and dl[0] > 1e-3:
                t = right / dl[0]
            else:
                continue
            y = h + t * dl[1]
            if 0.6 * h < y < 10 * h:
                heights.append(y)
    wall_h = float(np.median(heights)) if heights else height_m
    wall_h = _clamp(wall_h, max(1.2 * h, max([o["size"][1] for o in objects] + [0]) * 1.1), 8 * h)
    return {"yaw": psi, "left": left, "right": right, "back": back, "front": front, "height": wall_h,
            "floor_color": _hex(room.get("floor_color"), "#8a857c"),
            "wall_color": _hex(room.get("wall_color"), "#d8d4cb"),
            "walls_measured": sorted(lines), "wall_heights_measured": len(heights)}


LIGHT_INTENSITY, LIGHT_AMBIENT = 2.2, 0.55     # 뷰어 장면의 조명 (벽·바닥 색은 이 조명 기준으로 되돌린다)


def _srgb_to_lin(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, float) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _lin_to_hex(c: np.ndarray) -> str:
    c = np.clip(c, 0.0, 1.0)
    s = np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)
    return "#" + "".join(f"{int(round(v * 255)):02x}" for v in s)


def surface_colors(img: np.ndarray, cam: Cam, room: dict, objects: list[dict], sun: np.ndarray) -> dict:
    """복원한 방으로 픽셀마다 광선을 쏴 바닥·벽·배경을 나누고, 면마다 중앙값 색을 잰다.

    사진의 색은 이미 조명을 받은 색이므로, 뷰어의 조명(환경광 + 주광·cos)으로 나눠 재질 색으로 되돌린다.
    그러면 원래 시점에서 뷰어 화면이 사진과 비슷한 밝기로 보인다. (물체 색은 Codex 추정을 그대로 쓴다)
    """
    H, W = img.shape[:2]
    step = 2
    vs, us = np.mgrid[0:H:step, 0:W:step].astype(float) + 0.5
    a, b = (us - cam.cx) / cam.f, -(vs - cam.cy) / cam.f
    D = cam.F[None, None, :] + a[..., None] * cam.R[None, None, :] + b[..., None] * cam.U[None, None, :]
    cs, sn = math.cos(room["yaw"]), math.sin(room["yaw"])
    dx, dy, dz = D[..., 0] * cs - D[..., 2] * sn, D[..., 1], D[..., 0] * sn + D[..., 2] * cs
    inf = np.full(dx.shape, np.inf)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.stack([np.where(dy < -1e-9, -cam.h / dy, inf),
                      np.where(dz < -1e-9, room["back"] / dz, inf),
                      np.where(dx < -1e-9, room["left"] / dx, inf),
                      np.where(dx > 1e-9, room["right"] / dx, inf)])
    lab = np.argmin(t, axis=0)
    tmin = np.min(t, axis=0)
    y_hit = cam.h + np.where(np.isfinite(tmin), tmin, 0) * dy
    lab = np.where(~np.isfinite(tmin) | ((lab > 0) & (y_hit > room["height"])), 4, lab)   # 4 = 배경
    for o in objects:                                     # 물체와 그 둘레는 빼고 잰다
        pts, _ = shape_points(o["shape"], o["position"][0], o["position"][1], o["size"][0], o["size"][2],
                              o["size"][1], o.get("yaw", 0.0))
        u, v, z = cam.project(pts)
        if np.all(z > 0):
            m = 8
            lab[(us >= u.min() - m) & (us <= u.max() + m) & (vs >= v.min() - m) & (vs <= v.max() + m)] = -1
    k = 4                                                 # 경계에서 k칸(=8픽셀) 떨어진 곳만 쓴다
    ok = lab >= 0
    for sh in ((k, 0), (-k, 0), (0, k), (0, -k)):
        ok &= np.roll(lab, sh, axis=(0, 1)) == lab
    ok[:k], ok[-k:], ok[:, :k], ok[:, -k:] = False, False, False, False
    pix = img[::step, ::step][: lab.shape[0], : lab.shape[1]].astype(float)
    normals = {0: np.array([0.0, 1.0, 0.0]), 1: np.array([sn, 0.0, cs]),
               2: np.array([cs, 0.0, -sn]), 3: np.array([-cs, 0.0, sn])}
    out = {}
    names = {0: "floor", 1: "back", 2: "left", 3: "right", 4: "background"}
    for i, name in names.items():
        sel = ok & (lab == i)
        if sel.sum() < 150:
            continue
        med = np.median(pix[sel], axis=0)
        if name == "background":
            out[name] = "#" + "".join(f"{int(round(v)):02x}" for v in med)
            continue
        shade = (LIGHT_AMBIENT + LIGHT_INTENSITY * max(0.0, float(normals[i] @ sun))) / math.pi
        out[name] = _lin_to_hex(_srgb_to_lin(med) / shade)
    return out


def build_spec(a: dict, W: int, H: int, img: np.ndarray | None = None) -> dict:
    cam, cam_info = solve_camera(a, W, H)
    objects = []
    for o in (a.get("objects") or [])[:16]:
        if isinstance(o, dict):
            fit = fit_object(o, cam)
            if fit:
                objects.append(fit)
    room = solve_room(a, cam, objects)
    s = a.get("sun") or {}
    sun = np.array([_f(s.get("x"), 0.3), _f(s.get("y"), 1.0), _f(s.get("z"), 0.4)])
    if not np.all(np.isfinite(sun)) or np.linalg.norm(sun) < 1e-6:
        sun = np.array([0.3, 1.0, 0.4])
    sun /= np.linalg.norm(sun)
    if sun[1] < 0.2:
        sun[1] = 0.2
        sun /= np.linalg.norm(sun)
    background = _hex((a.get("room") or {}).get("background_color"), "#e9eef3")
    if img is not None:
        try:
            measured = surface_colors(img, cam, room, objects, sun)
        except Exception as e:                       # 색 측정은 보기용이라 실패해도 장면은 만든다
            log(f"면 색 측정 실패: {e}")
            measured = {}
        room["floor_color"] = measured.get("floor", room["floor_color"])
        room["wall_colors"] = {k: measured[k] for k in ("back", "left", "right") if k in measured}
        background = measured.get("background", background)
    p = math.radians(cam.pitch)
    camera = {"fov": cam.vfov, "aspect": W / H, "position": [0.0, cam.h, 0.0],
              "target": [0.0, cam.h + 5 * math.sin(p), -5 * math.cos(p)], "roll_deg": cam.roll}
    return {
        "version": 1, "image": {"width": W, "height": H}, "background": background,
        "camera": camera, "camera_info": cam_info, "room": room, "objects": objects,
        "light": {"direction": sun.round(4).tolist(), "intensity": LIGHT_INTENSITY, "ambient": LIGHT_AMBIENT},
    }


# ---------------------------------------------------------------------------
# 3. 장면 파일 (ES 모듈)
# ---------------------------------------------------------------------------

SCENE_BUILDER_JS = r"""
export function build(THREE) {
  const S = SPEC;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(S.background);
  const add = (parent, mesh, role, name) => {
    mesh.userData.role = role; mesh.name = name;
    mesh.castShadow = role === 'object'; mesh.receiveShadow = true;
    parent.add(mesh); return mesh;
  };
  const surface = (color) => new THREE.MeshStandardMaterial({ color, roughness: 0.95, metalness: 0 });

  // 방: 카메라 기준으로 놓고 방향(yaw)만큼 돌린다. 벽 법선은 방 안쪽을 본다.
  const r = S.room;
  const room = new THREE.Group(); room.name = 'room'; room.rotation.y = r.yaw; scene.add(room);
  const W = r.right - r.left, D = r.front - r.back, cx = (r.left + r.right) / 2, cz = (r.front + r.back) / 2;
  const floor = add(room, new THREE.Mesh(new THREE.PlaneGeometry(W, D), surface(r.floor_color)), 'ground', 'floor');
  floor.rotation.x = -Math.PI / 2; floor.position.set(cx, 0, cz);
  const wc = (side) => (r.wall_colors && r.wall_colors[side]) || r.wall_color;
  const back = add(room, new THREE.Mesh(new THREE.PlaneGeometry(W, r.height), surface(wc('back'))), 'wall', 'wall-back');
  back.position.set(cx, r.height / 2, r.back);
  const left = add(room, new THREE.Mesh(new THREE.PlaneGeometry(D, r.height), surface(wc('left'))), 'wall', 'wall-left');
  left.position.set(r.left, r.height / 2, cz); left.rotation.y = Math.PI / 2;
  const right = add(room, new THREE.Mesh(new THREE.PlaneGeometry(D, r.height), surface(wc('right'))), 'wall', 'wall-right');
  right.position.set(r.right, r.height / 2, cz); right.rotation.y = -Math.PI / 2;

  // 주요 물체: 물체 하나당 메시 하나
  S.objects.forEach((o, i) => {
    const [w, h, d] = o.size;
    const geo = o.shape === 'cylinder'
      ? new THREE.CylinderGeometry(w / 2, w / 2, h, 48)
      : new THREE.BoxGeometry(w, h, d);
    const m = add(scene, new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: o.color, roughness: 0.7 })),
                  'object', `object-${i + 1}-${o.shape}`);
    m.position.set(o.position[0], h / 2, o.position[1]);
    m.rotation.y = o.yaw || 0;
  });

  // 조명: 주광(방향광, 그림자) + 환경광
  scene.add(new THREE.AmbientLight(0xffffff, S.light.ambient));
  const sun = new THREE.DirectionalLight(0xffffff, S.light.intensity);
  const c = new THREE.Vector3(cx, 0, cz).applyAxisAngle(new THREE.Vector3(0, 1, 0), r.yaw);
  sun.position.copy(c).add(new THREE.Vector3(...S.light.direction).multiplyScalar(30));
  sun.target.position.copy(c);
  sun.castShadow = true; sun.shadow.mapSize.set(1024, 1024);
  const ext = Math.max(W, D, r.height) * 0.9;
  Object.assign(sun.shadow.camera, { left: -ext, right: ext, top: ext, bottom: -ext, near: 1, far: 80 });
  sun.shadow.bias = -0.0005;
  scene.add(sun, sun.target);

  // 카메라: 원본 사진과 같은 시점 (lookAt 다음 롤)
  const k = S.camera;
  const camera = new THREE.PerspectiveCamera(k.fov, k.aspect, 0.05, 500);
  camera.position.set(...k.position);
  camera.lookAt(new THREE.Vector3(...k.target));
  camera.rotateZ(k.roll_deg * Math.PI / 180);
  camera.updateProjectionMatrix(); camera.updateMatrixWorld(true);
  return { scene, camera };
}
"""


def scene_module(spec: dict, key: str) -> str:
    return (f"// tiny previz 장면 {key}. app.py가 Codex 이미지 분석 결과로 만들었다.\n"
            f"// 형식: export function build(THREE) -> {{ scene, camera }}\n"
            f"export const SPEC = {json.dumps(spec, ensure_ascii=False, indent=1)};\n"
            f"{SCENE_BUILDER_JS}")


# ---------------------------------------------------------------------------
# 4. 요청 저장소와 작업자
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        for sub in ("requests", "uploads", "scenes", "work"):
            (data_dir / sub).mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.records: dict[str, dict] = {}
        for f in (data_dir / "requests").glob("*.json"):
            try:
                self.records[f.stem] = json.loads(f.read_text())
            except ValueError:
                pass

    def get(self, key: str) -> dict | None:
        with self.lock:
            rec = self.records.get(key)
            return dict(rec) if rec else None

    def put(self, key: str, **fields) -> dict:
        with self.lock:
            rec = self.records.setdefault(key, {"key": key})
            rec.update(fields)
            tmp = self.dir / "requests" / f"{key}.json.tmp"
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
            tmp.replace(self.dir / "requests" / f"{key}.json")
            return dict(rec)

    def pending(self) -> list[str]:
        with self.lock:
            return [k for k, r in self.records.items() if r.get("status") == "pending"]


def normalize_image(raw: bytes, out: Path) -> tuple[int, int]:
    """업로드 이미지를 방향 보정·RGB·최대 크기 제한한 PNG로 저장한다."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")
    if max(img.size) > MAX_IMAGE_SIDE:
        s = MAX_IMAGE_SIDE / max(img.size)
        img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)
    img.save(out, "PNG")
    return img.width, img.height


class Worker:
    def __init__(self, store: Store):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="previz")

    def submit(self, key: str) -> None:
        self.pool.submit(self._run, key)

    def _run(self, key: str) -> None:
        rec = self.store.get(key) or {}
        work = self.store.dir / "work" / key
        work.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        try:
            image = work / "input.png"
            W, H = normalize_image((self.store.dir / "uploads" / rec["upload"]).read_bytes(), image)
        except Exception as e:
            log(f"[{key}] 이미지를 읽지 못했다: {e}")
            self.store.put(key, status="failed", error=f"이미지를 읽지 못했다: {e}", finished=time.time())
            return
        last = "알 수 없는 오류"
        for attempt in range(1, CODEX_TRIES + 1):
            try:
                analysis = run_codex(image, work, W, H)
                (work / f"analysis-{attempt}.json").write_text(json.dumps(analysis, indent=1, ensure_ascii=False))
                if not analysis.get("objects") and attempt < CODEX_TRIES:
                    raise RuntimeError("Codex가 주요 물체를 하나도 찾지 못했다")    # 한 번 더 물어본다
                spec = build_spec(analysis, W, H, np.asarray(Image.open(image).convert("RGB")))
                spec["source"] = {"codex_attempt": attempt, "seconds": round(time.time() - t0, 1)}
                out = self.store.dir / "scenes" / f"{key}.js"
                tmp = out.with_suffix(".js.tmp")
                tmp.write_text(scene_module(spec, key))
                tmp.replace(out)                               # 장면 파일이 다 쓰인 뒤에만 done
                self.store.put(key, status="done", error=None, finished=time.time(),
                               seconds=round(time.time() - t0, 1), objects=len(spec["objects"]))
                ci = spec["camera_info"]
                log(f"[{key}] done {time.time() - t0:.0f}초: fov {ci['vfov']:.1f} pitch {ci['pitch']:.1f} "
                    f"roll {ci['roll']:.1f}, 물체 {len(spec['objects'])}개, 벽 {spec['room']['walls_measured']}")
                return
            except Exception as e:
                last = str(e)[:500]
                log(f"[{key}] 시도 {attempt}/{CODEX_TRIES} 실패: {last}")
                (work / f"error-{attempt}.txt").write_text(traceback.format_exc())
        self.store.put(key, status="failed", error=last, finished=time.time())


# ---------------------------------------------------------------------------
# 5. HTTP
# ---------------------------------------------------------------------------

VIEWER_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>previz</title>
<link rel="icon" href="data:,">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;height:100%;background:#16181c;overflow:hidden;font:13px system-ui,sans-serif;color:#e8e8e8}
#stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
#wrap{position:relative;line-height:0}
#wrap canvas{display:block;touch-action:none;cursor:grab}
#ref{position:absolute;left:0;top:0;width:100%;height:100%;opacity:.5;pointer-events:none;display:none}
#hud{position:absolute;left:8px;top:8px;display:none;gap:6px;z-index:2}
body.ui #hud{display:flex}
#hud button{background:#000a;color:#fff;border:1px solid #fff4;border-radius:4px;padding:4px 8px;cursor:pointer;font:inherit}
#msg{position:absolute;left:0;right:0;top:45%;text-align:center;font-size:15px}
</style>
<script type="importmap">{"imports": {"three": "/vendor/three.module.js"}}</script>
</head><body>
<div id="stage"><div id="wrap"><img id="ref" alt=""></div></div>
<div id="hud"><button id="b-ref">원본 겹쳐 보기</button><button id="b-reset">원래 시점</button></div>
<div id="msg"></div>
<script type="module">
import * as THREE from 'three';

const key = new URLSearchParams(location.search).get('key') || '';
let resolveReady;
const P = window.__PREVIZ__ = { key, scene: null, camera: null, ready: new Promise((r) => (resolveReady = r)) };
const $ = (id) => document.getElementById(id);
const say = (t) => { $('msg').textContent = t; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
for (const ev of ['pointermove', 'touchstart', 'keydown']) addEventListener(ev, () => document.body.classList.add('ui'), { once: true });

async function waitDone() {
  if (!/^[a-f0-9]{8,32}$/.test(key)) { say('키가 올바르지 않다'); return false; }
  for (;;) {
    const res = await fetch(`/api/requests/${key}`, { cache: 'no-store' });
    const s = res.ok ? await res.json() : { status: 'missing' };
    if (s.status === 'done') return true;
    if (s.status === 'failed') { say('장면 생성 실패: ' + (s.error || '')); return false; }
    if (s.status !== 'pending') { say('알 수 없는 키'); return false; }
    say('장면 생성 중…');
    await sleep(2000);
  }
}

async function main() {
  if (!(await waitDone())) return;
  const { build } = await import(`/scenes/${key}.js`);
  const { scene, camera } = build(THREE);
  const aspect = camera.aspect;
  const home = { p: camera.position.clone(), q: camera.quaternion.clone() };
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  $('wrap').prepend(renderer.domElement);
  const render = () => renderer.render(scene, camera);
  const layout = () => {
    let w = innerWidth, h = innerHeight;
    if (w / h > aspect) w = Math.round(h * aspect); else h = Math.round(w / aspect);
    renderer.setSize(w, h);
    camera.aspect = aspect; camera.updateProjectionMatrix();
    render();
  };
  layout();
  addEventListener('resize', layout);
  say('');
  Object.assign(P, { scene, camera, renderer });
  resolveReady(P);

  // 원본 겹쳐 보기
  const ref = $('ref');
  ref.src = `/api/requests/${key}/image`;
  $('b-ref').onclick = () => { ref.style.display = ref.style.display === 'block' ? 'none' : 'block'; };

  // 간단한 궤도 보기: 끌어서 회전, 휠로 앞뒤, '원래 시점'으로 복귀
  const pivot = new THREE.Vector3();
  const objs = []; scene.traverse((o) => { if (o.isMesh && o.userData.role === 'object') objs.push(o); });
  if (objs.length) { objs.forEach((o) => pivot.add(o.position)); pivot.divideScalar(objs.length); }
  else pivot.copy(camera.position).add(camera.getWorldDirection(new THREE.Vector3()).multiplyScalar(4));
  pivot.y = Math.max(pivot.y, 0.3);
  let drag = null;
  const canvas = renderer.domElement;
  canvas.addEventListener('pointerdown', (e) => { drag = { x: e.clientX, y: e.clientY }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointerup', () => { drag = null; });
  canvas.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y; drag = { x: e.clientX, y: e.clientY };
    const off = camera.position.clone().sub(pivot);
    const sph = new THREE.Spherical().setFromVector3(off);
    sph.theta -= dx * 0.008; sph.phi = Math.min(Math.max(sph.phi - dy * 0.008, 0.05), Math.PI / 2 - 0.02);
    camera.position.copy(pivot).add(new THREE.Vector3().setFromSpherical(sph));
    camera.lookAt(pivot); render();
  });
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const off = camera.position.clone().sub(pivot).multiplyScalar(Math.exp(e.deltaY * 0.001));
    camera.position.copy(pivot).add(off); camera.lookAt(pivot); render();
  }, { passive: false });
  $('b-reset').onclick = () => { camera.position.copy(home.p); camera.quaternion.copy(home.q); render(); };
}

main().catch((e) => { say('뷰어 오류: ' + e.message); console.warn(e); });
</script>
</body></html>
"""

INDEX_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>tiny previz</title><link rel="icon" href="data:,">
<style>body{font:15px system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#222}
#out{margin-top:16px;white-space:pre-wrap}</style></head><body>
<h1>tiny previz</h1>
<p>사진(PNG/JPEG)을 올리면 같은 구도의 three.js 프리비즈 장면을 만든다.</p>
<input type="file" id="f" accept="image/png,image/jpeg">
<div id="out"></div>
<script>
const out = document.getElementById('out');
document.getElementById('f').onchange = async (e) => {
  const file = e.target.files[0]; if (!file) return;
  out.textContent = '업로드 중…';
  const res = await fetch('/api/requests', { method: 'POST', headers: { 'Content-Type': file.type || 'image/png' }, body: file });
  const body = await res.json();
  if (res.status !== 202) { out.textContent = '실패: ' + (body.error || res.status); return; }
  out.textContent = `키 ${body.key} — 생성 중…`;
  for (;;) {
    await new Promise((r) => setTimeout(r, 2000));
    const s = await (await fetch(`/api/requests/${body.key}`)).json();
    if (s.status === 'done') { location.href = `/view?key=${body.key}`; return; }
    if (s.status === 'failed') { out.textContent = `실패: ${s.error || ''}`; return; }
  }
};
</script></body></html>
"""


def make_handler(store: Store, worker: Worker):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "tiny-previz"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else (
                body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            if path != "/api/requests":
                self.rfile.read(length)
                return self._send(404, {"error": "not found"})
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                self.rfile.read(length)
                return self._send(400 if length <= 0 else 413, {"error": "이미지 본문이 비었거나 너무 크다"})
            raw = self.rfile.read(length)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                ext = "png"
            elif raw.startswith(b"\xff\xd8\xff"):
                ext = "jpg"
            else:
                return self._send(415, {"error": f"PNG 또는 JPEG 이미지가 아니다 (Content-Type {ctype or '없음'})"})
            key = uuid.uuid4().hex[:16]
            (store.dir / "uploads" / f"{key}.{ext}").write_bytes(raw)
            store.put(key, status="pending", error=None, created=time.time(), upload=f"{key}.{ext}",
                      content_type=ctype, bytes=len(raw))
            worker.submit(key)
            log(f"[{key}] 요청 접수 ({len(raw)} bytes, {ext})")
            return self._send(202, {"key": key})

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            if path == "/view":
                return self._send(200, VIEWER_HTML, "text/html; charset=utf-8")
            m = re.fullmatch(r"/api/requests/([^/]+)(/image)?", path)
            if m:
                key = m.group(1)
                rec = store.get(key) if KEY_RE.match(key) else None
                if rec is None:
                    return self._send(404, {"status": "failed", "error": "알 수 없는 키"})
                if m.group(2):
                    f = store.dir / "work" / key / "input.png"
                    if not f.exists():
                        f = store.dir / "uploads" / rec.get("upload", "")
                    if not f.is_file():
                        return self._send(404, {"error": "이미지 없음"})
                    ctype = "image/png" if f.suffix == ".png" else "image/jpeg"
                    return self._send(200, f.read_bytes(), ctype)
                body = {"status": rec.get("status", "pending")}
                if rec.get("error"):
                    body["error"] = rec["error"]
                return self._send(200, body)
            m = re.fullmatch(r"/scenes/([a-f0-9]{8,32})\.js", path)
            if m:
                f = store.dir / "scenes" / f"{m.group(1)}.js"
                if f.is_file():
                    return self._send(200, f.read_bytes(), "text/javascript; charset=utf-8")
                return self._send(404, "// not found", "text/javascript; charset=utf-8")
            m = re.fullmatch(r"/vendor/([\w.]+)", path)
            if m and m.group(1) in VENDOR_FILES and (VENDOR / m.group(1)).is_file():
                return self._send(200, (VENDOR / m.group(1)).read_bytes(), "text/javascript; charset=utf-8",
                                  {"Cache-Control": "max-age=3600"})
            return self._send(404, {"error": "not found"})

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="tiny previz: 사진 → three.js 프리비즈 장면")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    store = Store(data_dir)
    worker = Worker(store)
    for key in store.pending():                      # 재시작 전에 끝나지 못한 요청을 이어서 처리한다
        log(f"[{key}] 재시작 후 다시 처리")
        worker.submit(key)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, worker))
    server.daemon_threads = True

    def stop(signum, frame):
        RUNNING.kill_all()
        os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"tiny previz: http://{args.host}:{args.port}  data={data_dir}  codex={shutil.which('codex')}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
