"""방 상자 모델: 변수 x로 카메라 축·초점거리, 방 모서리의 3D 위치와 영상 직선, 연직 소실점을 낸다.

변수 x = (화각, 피치, 롤, 방 yaw (도), log 왼벽 거리, log 오른벽 거리, log 뒷벽 거리, log 벽 높이).
길이는 카메라 높이 단위.
방 좌표는 previz/geometry/room.py의 RoomFrame과 같다 (카메라는 (0, 1, 0)에서 방 yaw만큼 돌아 -z를 본다).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..ellipses import up_in_camera

FAR = 60.0                    # 보이지 않는 벽은 이만큼 멀리 둔다 (카메라 높이 단위)
NEAR = 0.05                   # 카메라 앞 이만큼부터 투영한다

EDGES = {   # 이름 → (필요한 벽, 윗변인지)
    "back_bottom": (("back",), False), "back_top": (("back",), True),
    "left_bottom": (("left",), False), "left_top": (("left",), True),
    "right_bottom": (("right",), False), "right_top": (("right",), True),
    "corner_left": (("back", "left"), False), "corner_right": (("back", "right"), False),
}


def rotation(fov: float, pitch: float, roll: float, yaw: float):
    p, r, y = math.radians(pitch), math.radians(roll), math.radians(yaw)
    F = np.array([math.sin(y) * math.cos(p), math.sin(p), -math.cos(y) * math.cos(p)])
    r0 = np.array([math.cos(y), 0.0, math.sin(y)])
    u0 = np.array([-math.sin(y) * math.sin(p), math.cos(p), math.cos(y) * math.sin(p)])
    return F, math.cos(r) * r0 + math.sin(r) * u0, -math.sin(r) * r0 + math.cos(r) * u0


def focal_px(H: int, fov: float) -> float:
    """수직 화각(도) → 초점거리 (픽셀)."""
    return (H / 2) / math.tan(math.radians(fov) / 2)


def bounded_focal_px(H: int, fov: float) -> float:
    """맞추는 도중의 초점거리: 화각을 10~140°로 묶어 잰다 (풀이가 화각을 멀리 보내도 투영이 깨지지 않게)."""
    return focal_px(H, min(max(fov, 10.0), 140.0))


def camera_up(x) -> np.ndarray:
    """x의 (화각, 피치, 롤)에서 월드 연직 방향을 카메라 좌표(오른쪽, 아래, 앞)로 (방 yaw는 상관없다)."""
    F, R, U = rotation(x[0], x[1], x[2], 0.0)
    return up_in_camera(F, R, U)


@dataclass
class RoomModel:
    W: int
    H: int
    walls: frozenset               # 보이는 벽 ("back", "left", "right")
    has_top: bool                  # 벽 윗변이 보이는지

    def edges(self) -> list[str]:
        out = []
        for name, (need, top) in EDGES.items():
            if all(w in self.walls for w in need) and (self.has_top or not top):
                out.append(name)
        return out

    def dims(self, x):
        xl = math.exp(x[4]) if "left" in self.walls else FAR
        xr = math.exp(x[5]) if "right" in self.walls else FAR
        zb = math.exp(x[6]) if "back" in self.walls else FAR
        hw = math.exp(x[7]) if self.has_top else FAR
        return xl, xr, zb, hw

    def edge_3d(self, x, name: str):
        xl, xr, zb, hw = self.dims(x)
        top = min(hw, FAR)
        return {"back_bottom": ([-xl, 0, -zb], [xr, 0, -zb]), "back_top": ([-xl, hw, -zb], [xr, hw, -zb]),
                "left_bottom": ([-xl, 0, -zb], [-xl, 0, FAR]), "left_top": ([-xl, hw, -zb], [-xl, hw, FAR]),
                "right_bottom": ([xr, 0, -zb], [xr, 0, FAR]), "right_top": ([xr, hw, -zb], [xr, hw, FAR]),
                "corner_left": ([-xl, 0, -zb], [-xl, top, -zb]),
                "corner_right": ([xr, 0, -zb], [xr, top, -zb])}[name]

    def camera(self, x):
        F, R, U = rotation(x[0], x[1], x[2], x[3])
        return F, R, U, bounded_focal_px(self.H, x[0])

    def project(self, x, pts):
        F, R, U, f = self.camera(x)
        v = np.asarray(pts, float) - np.array([0.0, 1.0, 0.0])
        z = v @ F
        zs = np.where(z > 1e-6, z, 1e-6)
        return np.stack([self.W / 2 + f * (v @ R) / zs, self.H / 2 - f * (v @ U) / zs], -1), z

    def segment(self, x, name: str):
        """모서리의 카메라 앞 부분을 투영한 선분 (화면 밖까지). 카메라 앞에 없으면 None."""
        a, b = (np.array(p, float) for p in self.edge_3d(x, name))
        F = self.camera(x)[0]
        C = np.array([0.0, 1.0, 0.0])
        za, zb = (a - C) @ F, (b - C) @ F
        if za < NEAR and zb < NEAR:
            return None
        if za < NEAR:
            a = a + (b - a) * (NEAR - za) / (zb - za)
        elif zb < NEAR:
            b = a + (b - a) * (NEAR - za) / (zb - za)
        P, _ = self.project(x, np.stack([a, b]))
        if not np.all(np.isfinite(P)) or np.linalg.norm(P[1] - P[0]) < 1e-6:
            return None
        return P[0], P[1]

    def line(self, x, name: str):
        seg = self.segment(x, name)
        if seg is None:
            return None
        d = seg[1] - seg[0]
        n = np.array([-d[1], d[0]]) / np.linalg.norm(d)
        return n, float(n @ seg[0])

    def vertical_vp(self, x):
        """연직 방향의 소실점 (동차 좌표 u, v, w)."""
        F, R, U, f = self.camera(x)
        dx, dy, dz = R[1], U[1], F[1]
        return np.array([self.W / 2 * dz + f * dx, self.H / 2 * dz - f * dy, dz])

    def vertical_line(self, x, m):
        """점 m을 지나는 연직선(3D)의 영상 직선 (n, c)."""
        vp = self.vertical_vp(x)
        t = vp[:2] - np.asarray(m) * vp[2]
        nt = float(np.linalg.norm(t))
        if nt < 1e-12:
            return None
        t = t / nt
        n = np.array([-t[1], t[0]])
        return n, float(n @ m)
