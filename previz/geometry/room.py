"""방: 벽과 바닥의 경계선을 바닥으로 역투영해 방 방향·벽 위치를 풀고, 벽 윗변으로 벽 높이를 구한다."""

from __future__ import annotations

import math

import numpy as np

from .camera import Cam
from .values import clamp, points, to_float, to_hex

SIDES = ("back", "left", "right")


class RoomFrame:
    """방 로컬 좌표: 월드를 방 방향(yaw)만큼 돌린 좌표. 뒷벽은 로컬 x축, 옆벽은 로컬 z축과 나란하다."""

    def __init__(self, yaw: float):
        self.yaw = yaw
        self.cs, self.sn = math.cos(yaw), math.sin(yaw)

    def xz(self, p):
        """월드 점·방향 p (p[0] = x, p[2] = z) → 로컬 (x, z). 배열이면 첫 축이 xyz다."""
        return p[0] * self.cs - p[2] * self.sn, p[0] * self.sn + p[2] * self.cs


def wall_edges(a: dict, cam: Cam) -> tuple[dict, dict]:
    """(면 → 바닥으로 역투영한 바닥선 양 끝, 면 → 윗변 픽셀 점). 면마다 바닥선이 처음 잡힌 항목을 쓴다."""
    h = cam.h
    lines: dict[str, list[np.ndarray]] = {}
    tops: dict[str, list[tuple[float, float]]] = {}
    for wall in a.get("walls") or []:
        side = str(wall.get("side", ""))
        if side not in SIDES or side in lines:
            continue
        ground = (cam.ground(u, v, max_dist=60 * h) for u, v in points(wall.get("bottom_edge")))
        pts = [g for g in ground if g is not None]
        if len(pts) >= 2 and np.linalg.norm(pts[-1] - pts[0]) > 0.05 * h:
            lines[side] = [pts[0], pts[-1]]
        tops[side] = points(wall.get("top_edge"))
    return lines, tops


def room_yaw(lines: dict) -> float:
    """방의 방향(yaw): 뒷벽은 로컬 x축, 옆벽은 로컬 z축과 나란하다. 50° 넘게 돌아간 선은 버린다."""
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
    return float(np.median(yaws)) if yaws else 0.0


def _side_mean(lines: dict, side: str, axis: int, frame: RoomFrame) -> float | None:
    """측정된 벽 바닥선 양 끝의 로컬 좌표 평균 (axis 0 = x, 1 = z). 그 면이 없으면 None."""
    if side not in lines:
        return None
    return float(np.mean([frame.xz(p)[axis] for p in lines[side]]))


def measured_walls(lines: dict, frame: RoomFrame, h: float) -> tuple:
    """(뒷벽 z, 왼벽 x, 오른벽 x). 카메라에서 맞는 쪽에 충분히 떨어져 있지 않은 벽은 None."""
    back = _side_mean(lines, "back", 1, frame)
    left = _side_mean(lines, "left", 0, frame)
    right = _side_mean(lines, "right", 0, frame)
    return (back if back is not None and back < -0.3 * h else None,
            left if left is not None and left < -0.1 * h else None,
            right if right is not None and right > 0.1 * h else None)


def room_bounds(walls: tuple, objects: list[dict], frame: RoomFrame, h: float, width_m: float,
                depth_m: float) -> tuple[float, float, float]:
    """(뒷벽 z, 왼벽 x, 오른벽 x). 못 잰 벽은 물체 배치와 Codex 방 크기로 채우고, 물체가 벽 밖으로 나가지 않게 넓힌다."""
    back, left, right = walls
    obj_local = [frame.xz([o["position"][0], 0.0, o["position"][1]]) for o in objects]
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
    for (lx, lz), r in zip(obj_local, obj_r):
        left, right, back = min(left, lx - r), max(right, lx + r), min(back, lz - r)
    return back, left, right


def _top_edge_height(cam: Cam, frame: RoomFrame, side: str, u: float, v: float, bounds: tuple) -> float | None:
    """벽 윗변의 점 하나를 그 벽 평면과 만나게 해 높이를 구한다. 광선이 그 벽 쪽으로 가지 않으면 None."""
    back, left, right = bounds
    d = cam.ray(u, v)
    dx, dz = frame.xz(d)
    if side == "back" and dz < -1e-3:
        t = back / dz
    elif side == "left" and dx < -1e-3:
        t = left / dx
    elif side == "right" and dx > 1e-3:
        t = right / dx
    else:
        return None
    return cam.h + t * d[1]


def wall_height(cam: Cam, frame: RoomFrame, tops: dict, bounds: tuple, objects: list[dict], default: float) -> tuple:
    """(벽 높이, 높이를 잰 윗변 점 수). 잰 값이 없으면 Codex 방 높이. 카메라와 가장 높은 물체보다는 높게 둔다."""
    h = cam.h
    heights = []
    for side, pts in tops.items():
        for u, v in pts:
            y = _top_edge_height(cam, frame, side, u, v, bounds)
            if y is not None and 0.6 * h < y < 10 * h:
                heights.append(y)
    wall_h = float(np.median(heights)) if heights else default
    wall_h = clamp(wall_h, max(1.2 * h, max([o["size"][1] for o in objects] + [0]) * 1.1), 8 * h)
    return wall_h, len(heights)


def solve_room(a: dict, cam: Cam, objects: list[dict]) -> dict:
    h = cam.h
    room = a.get("room") or {}
    width_m = clamp(to_float(room.get("width_m"), 5.0), 1.5, 40.0)
    depth_m = clamp(to_float(room.get("depth_m"), 6.0), 1.5, 60.0)
    height_m = clamp(to_float(room.get("height_m"), 2.8), 1.0, 20.0)
    lines, tops = wall_edges(a, cam)
    frame = RoomFrame(room_yaw(lines))
    back, left, right = room_bounds(measured_walls(lines, frame, h), objects, frame, h, width_m, depth_m)
    front = max(1.0 * h, 0.4)
    wall_h, n_heights = wall_height(cam, frame, tops, (back, left, right), objects, height_m)
    return {"yaw": frame.yaw, "left": left, "right": right, "back": back, "front": front, "height": wall_h,
            "floor_color": to_hex(room.get("floor_color"), "#8a857c"),
            "wall_color": to_hex(room.get("wall_color"), "#d8d4cb"),
            "walls_measured": sorted(lines), "wall_heights_measured": n_heights}
