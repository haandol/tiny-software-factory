"""장면 명세 조립: 카메라 보정 → 물체 피팅 → 방 → 주광 → 면 색을 차례로 풀어 뷰어가 그릴 명세(dict)를 만든다."""

from __future__ import annotations

import math

import numpy as np

from ..config import log
from .calibration import solve_camera
from .camera import Cam
from .colors import LIGHT_AMBIENT, LIGHT_INTENSITY, surface_colors
from .objects import fit_object
from .room import solve_room
from .values import clamp, to_float, to_hex

MAX_OBJECTS = 16
DEFAULT_SUN = (0.3, 1.0, 0.4)


def fit_objects(a: dict, cam: Cam) -> list[dict]:
    """측정값의 물체마다 맞춘 명세. "index"는 측정값 물체 번호 (맞추지 못한 물체는 빠진다)."""
    objects = []
    for i, o in enumerate((a.get("objects") or [])[:MAX_OBJECTS]):
        if isinstance(o, dict):
            fit = fit_object(o, cam)
            if fit:
                objects.append({**fit, "index": i})
    return objects


def sun_direction(a: dict) -> np.ndarray:
    """주광 방향 단위 벡터. 값이 없으면 기본값, 너무 낮으면 높이 성분을 0.2로 올린다."""
    s = a.get("sun") or {}
    sun = np.array([to_float(s.get("x"), 0.3), to_float(s.get("y"), 1.0), to_float(s.get("z"), 0.4)])
    if not np.all(np.isfinite(sun)) or np.linalg.norm(sun) < 1e-6:
        sun = np.array(DEFAULT_SUN)
    sun /= np.linalg.norm(sun)
    if sun[1] < 0.2:
        sun[1] = 0.2
        sun /= np.linalg.norm(sun)
    return sun


def apply_measured_colors(img: np.ndarray, cam: Cam, room: dict, objects: list[dict], sun: np.ndarray,
                          background: str) -> str:
    """사진에서 잰 바닥·벽 색을 room에 넣고, 배경 색을 돌려준다. 측정이 실패하면 Codex 색을 그대로 둔다."""
    try:
        measured = surface_colors(img, cam, room, objects, sun)
    except Exception as e:                       # 색 측정은 보기용이라 실패해도 장면은 만든다
        log(f"면 색 측정 실패: {e}")
        measured = {}
    room["floor_color"] = measured.get("floor", room["floor_color"])
    room["wall_colors"] = {k: measured[k] for k in ("back", "left", "right") if k in measured}
    return measured.get("background", background)


def camera_spec(cam: Cam, W: int, H: int) -> dict:
    """three.js 카메라: 위치, 바라보는 점(피치), 그 다음 돌릴 롤."""
    p = math.radians(cam.pitch)
    return {"fov": cam.vfov, "aspect": W / H, "position": [0.0, cam.h, 0.0],
            "target": [0.0, cam.h + 5 * math.sin(p), -5 * math.cos(p)], "roll_deg": cam.roll}


def fixed_camera(a: dict, W: int, H: int, camera: dict) -> tuple[Cam, dict]:
    """사진으로 맞춘 카메라(화각·피치·롤, previz/geometry/roomfit/)를 이 측정값의 카메라 높이로 쓴다."""
    height = clamp(to_float((a.get("camera") or {}).get("height_m"), 1.5), 0.3, 5.0)
    vfov = clamp(float(camera["vfov"]), 15.0, 120.0)
    pitch, roll = clamp(float(camera["pitch"]), -60.0, 45.0), clamp(float(camera["roll"]), -30.0, 30.0)
    info = {"vfov": vfov, "pitch": pitch, "roll": roll, "source": camera.get("source", "image"),
            **{k: v for k, v in camera.items() if k not in ("vfov", "pitch", "roll", "source")}}
    return Cam(W, H, vfov, pitch, roll, height), info


def scene_camera(a: dict, W: int, H: int, camera: dict | None = None) -> tuple[Cam, dict]:
    """camera(화각·피치·롤)가 있으면 그 카메라를 이 측정값의 카메라 높이로 쓰고, 없으면 이 측정값 하나로 보정한다."""
    return fixed_camera(a, W, H, camera) if camera else solve_camera(a, W, H)


def build_spec(a: dict, W: int, H: int, img: np.ndarray | None = None, camera: dict | None = None,
               objects: list[dict] | None = None) -> dict:
    """camera(화각·피치·롤)가 있으면 그 카메라를 쓰고, 없으면 이 측정값 하나로 카메라를 보정한다.
    objects가 있으면 측정값의 물체를 맞추지 않고 그 물체(이 측정값의 카메라 높이 단위)를 쓴다."""
    cam, cam_info = scene_camera(a, W, H, camera)
    objects = fit_objects(a, cam) if objects is None else objects
    room = solve_room(a, cam, objects)
    sun = sun_direction(a)
    background = to_hex((a.get("room") or {}).get("background_color"), "#e9eef3")
    if img is not None:
        background = apply_measured_colors(img, cam, room, objects, sun, background)
    return {
        "version": 1, "image": {"width": W, "height": H}, "background": background,
        "camera": camera_spec(cam, W, H), "camera_info": cam_info, "room": room, "objects": objects,
        "light": {"direction": sun.round(4).tolist(), "intensity": LIGHT_INTENSITY, "ambient": LIGHT_AMBIENT},
    }
