"""장면 생성기: 시드 하나 → 정답 장면. 기준 앱이 쓰는 빈 장면과 오차 주입도 여기 둔다."""

from __future__ import annotations

import json
import math
import random

from .policy import IMAGE_H, IMAGE_W

PALETTE_OBJECT = ["#d1495b", "#2e86ab", "#f4a259", "#5b8e7d", "#8e6bbf", "#edae49", "#3d5a80", "#e07a5f"]
PALETTE_FLOOR = ["#8d8478", "#a39e93", "#7a7f80", "#9c8f80"]
PALETTE_WALL = ["#d9d4c7", "#c9ccd1", "#e3dccf", "#cfd6cf"]


def norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def sub(a, b):
    return [x - y for x, y in zip(a, b)]


def _place_objects(rng: random.Random, W: float, D: float, cam: list, visible) -> list[dict]:
    """서로 겹치지 않고 카메라에 보이는 위치에 물체 1~4개를 놓는다."""
    objects, colors = [], rng.sample(PALETTE_OBJECT, 4)
    for _ in range(rng.randint(1, 4)):
        for _attempt in range(300):
            shape = rng.choice(["box", "cylinder"])
            if shape == "box":
                size = [rng.uniform(0.4, 1.4), rng.uniform(0.3, 1.6), rng.uniform(0.4, 1.4)]
            else:
                dia = rng.uniform(0.4, 1.2)
                size = [dia, rng.uniform(0.4, 1.6), dia]
            radius = max(size[0], size[2]) * 0.75
            x = rng.uniform(-W / 2 + radius, W / 2 - radius)
            z = rng.uniform(-D / 2 + radius, cam[2] - 1.2)
            if not (visible([x, size[1] / 2, z]) and visible([x, size[1], z])):
                continue
            if any(math.dist([x, z], o["position"]) < radius + o["_r"] + 0.1 for o in objects):
                continue
            objects.append({"shape": shape, "size": size, "position": [x, z],
                            "yaw_deg": rng.uniform(0, 90) if shape == "box" else 0.0,
                            "color": colors[len(objects)], "_r": radius})
            break
    for o in objects:
        o.pop("_r")
    return objects


def generate_scene(seed: int, variant: str) -> dict:
    """방, 카메라, 물체 1~4개, 조명을 시드로 정한다. 같은 시드는 항상 같은 장면이 된다."""
    rng = random.Random(seed)
    W, D, H = rng.uniform(4, 8), rng.uniform(4, 8), rng.uniform(2.6, 3.4)
    fov = rng.uniform(35, 75)
    pitch = math.radians(rng.uniform(-15, 3))
    yaw = math.radians(rng.uniform(-20, 20))
    roll = rng.uniform(-5, 5) if variant == "holdout" else rng.uniform(-3, 3)
    cam = [rng.uniform(-W * 0.2, W * 0.2), rng.uniform(1.0, 1.8), D / 2 - rng.uniform(0.2, 0.8)]
    forward = [math.sin(yaw) * math.cos(pitch), math.sin(pitch), -math.cos(yaw) * math.cos(pitch)]
    target = [cam[i] + forward[i] * 5 for i in range(3)]

    right = norm(cross(forward, [0, 1, 0]))
    up = cross(right, forward)
    tan_v = math.tan(math.radians(fov) / 2)
    tan_h = tan_v * IMAGE_W / IMAGE_H

    def visible(p):
        v = sub(p, cam)
        z = dot(v, forward)
        if z < 1.0:
            return False
        return abs(dot(v, right) / (z * tan_h)) < 0.8 and abs(dot(v, up) / (z * tan_v)) < 0.8

    objects = _place_objects(rng, W, D, cam, visible)
    if not objects:  # 드물게 배치에 실패하면 다른 시드로 대신한다
        return generate_scene(seed + 7919, variant) | {"seed": seed}

    light_dir = norm([rng.uniform(-1, 1), rng.uniform(1.5, 3), rng.uniform(0.3, 1.2)])
    lo, hi = (1.2, 3.4) if variant == "holdout" else (1.8, 2.8)
    return {
        "seed": seed, "variant": variant,
        "image": {"width": IMAGE_W, "height": IMAGE_H},
        "background": "#e9eef3",
        "room": {"width": W, "depth": D, "height": H,
                 "floor_color": rng.choice(PALETTE_FLOOR), "wall_color": rng.choice(PALETTE_WALL)},
        "camera": {"fov": fov, "position": cam, "target": target, "roll_deg": roll},
        "objects": objects,
        "light": {"direction": light_dir, "intensity": rng.uniform(lo, hi), "ambient": rng.uniform(0.35, 0.6)},
    }


EMPTY_SPEC = {
    "seed": 0, "image": {"width": IMAGE_W, "height": IMAGE_H}, "background": "#e9eef3",
    "room": {"width": 6, "depth": 6, "height": 3, "floor_color": "#8d8478", "wall_color": "#d9d4c7"},
    "camera": {"fov": 50, "position": [0, 1.6, 2.5], "target": [0, 1.2, -3], "roll_deg": 0},
    "objects": [], "light": {"direction": [0.3, 0.9, 0.3], "intensity": 2.0, "ambient": 0.5},
}


def perturb(spec: dict, level: float) -> dict:
    """정답에 정해진 방향으로 오차를 더한다. level이 클수록 모든 항목이 같은 쪽으로 더 틀린다."""
    s = json.loads(json.dumps(spec))
    s["camera"]["fov"] = min(110.0, s["camera"]["fov"] * (1 + level))
    s["camera"]["target"][1] -= level * 2.5
    s["camera"]["roll_deg"] += level * 10
    for o in s["objects"]:
        o["position"][0] += level * 1.0
        o["size"] = [x * (1 + level) for x in o["size"]]
        rgb = [int(o["color"][i:i + 2], 16) for i in (1, 3, 5)]
        o["color"] = "#" + "".join(f"{round(c + (128 - c) * level):02x}" for c in rgb)
    return s
