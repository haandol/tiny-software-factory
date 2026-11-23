"""물체 모양(상자·원기둥)의 3D 꼭짓점: 투영해 bbox에 맞추거나 화면에서 가릴 영역을 구할 때 쓴다."""

from __future__ import annotations

import math

import numpy as np

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


def object_points(o: dict):
    """피팅한 물체 명세({"shape", "position", "size", "yaw"})의 (전체 점, 바닥 모서리 점)."""
    return shape_points(o["shape"], o["position"][0], o["position"][1], o["size"][0], o["size"][2],
                        o["size"][1], o.get("yaw", 0.0))
