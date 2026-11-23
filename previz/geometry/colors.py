"""색: sRGB↔선형 변환, Codex 물체 색 보정, 사진에서 바닥·벽·배경 면 색을 재서 뷰어 조명 기준 재질 색으로 되돌리기."""

from __future__ import annotations

import math

import numpy as np

from .camera import Cam
from .room import RoomFrame
from .shapes import object_points

LIGHT_INTENSITY, LIGHT_AMBIENT = 2.2, 0.55     # 뷰어 장면의 조명 (벽·바닥 색은 이 조명 기준으로 되돌린다)

# Codex가 말하는 물체 색에는 음영이 남아 있어 실제 재질 색보다 어둡다 (공개·홀드아웃 26개 물체에서
# 선형 밝기 1.2배가 ΔE를 9.8 → 7.4로 줄였다). 장면 전체에 같은 보정을 한 번 곱한다.
CODEX_COLOR_GAIN = 1.2

STEP = 2                 # 면 색은 STEP 픽셀 간격으로 잰다
EDGE = 4                 # 경계에서 EDGE칸(=8픽셀) 떨어진 곳만 쓴다
OBJECT_MARGIN = 8        # 물체 bbox 둘레로 이만큼(픽셀) 더 뺀다
MIN_PIXELS = 150         # 면 하나에 이보다 적은 칸이 남으면 재지 않는다
SURFACES = {0: "floor", 1: "back", 2: "left", 3: "right", 4: "background"}


def srgb_to_lin(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, float) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def lin_to_hex(c: np.ndarray) -> str:
    c = np.clip(c, 0.0, 1.0)
    s = np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)
    return "#" + "".join(f"{int(round(v * 255)):02x}" for v in s)


def albedo_from_codex(hex_color: str) -> str:
    rgb = np.array([int(hex_color[i:i + 2], 16) for i in (1, 3, 5)], float)
    return lin_to_hex(srgb_to_lin(rgb) * CODEX_COLOR_GAIN)


def _label_rays(cam: Cam, room: dict, H: int, W: int):
    """STEP 간격 픽셀마다 광선을 쏴 처음 닿는 면 번호(SURFACES)를 매긴다. (번호, 픽셀 u, 픽셀 v)."""
    vs, us = np.mgrid[0:H:STEP, 0:W:STEP].astype(float) + 0.5
    a, b = (us - cam.cx) / cam.f, -(vs - cam.cy) / cam.f
    D = cam.F[None, None, :] + a[..., None] * cam.R[None, None, :] + b[..., None] * cam.U[None, None, :]
    dx, dz = RoomFrame(room["yaw"]).xz(np.moveaxis(D, -1, 0))
    dy = D[..., 1]
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
    return lab, us, vs


def _mask_objects(lab: np.ndarray, us: np.ndarray, vs: np.ndarray, cam: Cam, objects: list[dict]) -> None:
    """물체와 그 둘레는 빼고 잰다 (번호 -1)."""
    m = OBJECT_MARGIN
    for o in objects:
        pts, _ = object_points(o)
        u, v, z = cam.project(pts)
        if np.all(z > 0):
            lab[(us >= u.min() - m) & (us <= u.max() + m) & (vs >= v.min() - m) & (vs <= v.max() + m)] = -1


def _interior(lab: np.ndarray) -> np.ndarray:
    """면 경계와 화면 가장자리에서 EDGE칸 넘게 떨어진 칸."""
    k = EDGE
    ok = lab >= 0
    for sh in ((k, 0), (-k, 0), (0, k), (0, -k)):
        ok &= np.roll(lab, sh, axis=(0, 1)) == lab
    ok[:k], ok[-k:], ok[:, :k], ok[:, -k:] = False, False, False, False
    return ok


def surface_colors(img: np.ndarray, cam: Cam, room: dict, objects: list[dict], sun: np.ndarray) -> dict:
    """복원한 방으로 픽셀마다 광선을 쏴 바닥·벽·배경을 나누고, 면마다 중앙값 색을 잰다.

    사진의 색은 이미 조명을 받은 색이므로, 뷰어의 조명(환경광 + 주광·cos)으로 나눠 재질 색으로 되돌린다.
    그러면 원래 시점에서 뷰어 화면이 사진과 비슷한 밝기로 보인다. (물체 색은 Codex 추정을 그대로 쓴다)
    """
    H, W = img.shape[:2]
    lab, us, vs = _label_rays(cam, room, H, W)
    _mask_objects(lab, us, vs, cam, objects)
    ok = _interior(lab)
    pix = img[::STEP, ::STEP][: lab.shape[0], : lab.shape[1]].astype(float)
    cs, sn = math.cos(room["yaw"]), math.sin(room["yaw"])
    normals = {0: np.array([0.0, 1.0, 0.0]), 1: np.array([sn, 0.0, cs]),
               2: np.array([cs, 0.0, -sn]), 3: np.array([-cs, 0.0, sn])}
    out = {}
    for i, name in SURFACES.items():
        sel = ok & (lab == i)
        if sel.sum() < MIN_PIXELS:
            continue
        med = np.median(pix[sel], axis=0)
        if name == "background":
            out[name] = "#" + "".join(f"{int(round(v)):02x}" for v in med)
            continue
        shade = (LIGHT_AMBIENT + LIGHT_INTENSITY * max(0.0, float(normals[i] @ sun))) / math.pi
        out[name] = lin_to_hex(srgb_to_lin(med) / shade)
    return out
