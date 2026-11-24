"""장면 생성기: 시드 하나 → 정답 장면. 기준 앱이 쓰는 빈 장면과 오차 주입도 여기 둔다."""

from __future__ import annotations

import json
import math
import random

from .policy import IMAGE_H, IMAGE_W

PALETTE_OBJECT = ["#d1495b", "#2e86ab", "#f4a259", "#5b8e7d", "#8e6bbf", "#edae49", "#3d5a80", "#e07a5f"]
PALETTE_FLOOR = ["#8d8478", "#a39e93", "#7a7f80", "#9c8f80"]
PALETTE_WALL = ["#d9d4c7", "#c9ccd1", "#e3dccf", "#cfd6cf"]

# 가림 판정 (SWF-2). 물체마다 화면 안 실루엣 중 다른 물체에 가려지지 않은 비율을 광선 격자로 잰다.
# 어느 물체든 이 비율이 하한보다 작아지는 배치는 받아들이지 않는다 (= 대부분 가려진 물체는 만들지 않는다).
MIN_VISIBLE_FRACTION = 0.5
VIS_GRID = (160, 120)                   # 광선 격자 (재투영 마스크와 같은 해상도)


def norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def sub(a, b):
    return [x - y for x, y in zip(a, b)]


def _hit_box(o: list, d: list, ob: dict) -> float | None:
    """광선 o + t·d가 (y축으로 yaw만큼 돈) 상자에 처음 닿는 t. 안 닿으면 None."""
    (px, pz), (sx, sy, sz) = ob["position"], ob["size"]
    a = math.radians(ob["yaw_deg"])
    c, s = math.cos(a), math.sin(a)
    ox, oy, oz = o[0] - px, o[1] - sy / 2, o[2] - pz      # 월드 → 상자 좌표 (three.js rotation.y의 역회전)
    lo = (c * ox - s * oz, oy, s * ox + c * oz)
    ld = (c * d[0] - s * d[2], d[1], s * d[0] + c * d[2])
    t0, t1 = 0.0, math.inf
    for i, half in enumerate((sx / 2, sy / 2, sz / 2)):
        if abs(ld[i]) < 1e-12:
            if abs(lo[i]) > half:
                return None
            continue
        ta, tb = (-half - lo[i]) / ld[i], (half - lo[i]) / ld[i]
        t0, t1 = max(t0, min(ta, tb)), min(t1, max(ta, tb))
        if t0 > t1:
            return None
    return t0


def _hit_cylinder(o: list, d: list, ob: dict) -> float | None:
    """광선 o + t·d가 바닥에 선 원기둥(옆면·윗면·밑면)에 처음 닿는 t. 안 닿으면 None."""
    (px, pz), (dia, h, _) = ob["position"], ob["size"]
    r2 = (dia / 2) ** 2
    ox, oz = o[0] - px, o[2] - pz
    best = None
    a = d[0] * d[0] + d[2] * d[2]
    if a > 1e-12:
        b = ox * d[0] + oz * d[2]
        disc = b * b - a * (ox * ox + oz * oz - r2)
        if disc >= 0:
            t = (-b - math.sqrt(disc)) / a
            if t > 0 and 0.0 <= o[1] + t * d[1] <= h:
                best = t
    if abs(d[1]) > 1e-12:
        for y in (h, 0.0):
            t = (y - o[1]) / d[1]
            if t > 0 and (ox + t * d[0]) ** 2 + (oz + t * d[2]) ** 2 <= r2 and (best is None or t < best):
                best = t
    return best


def _screen_rect(ob: dict, view: dict) -> tuple[int, int, int, int]:
    """물체를 감싸는 기둥의 꼭짓점 8개를 투영한 화면 사각형 (격자 칸 번호, 양끝 포함)."""
    w, h = VIS_GRID
    (px, pz), size = ob["position"], ob["size"]
    rad = math.hypot(size[0], size[2]) / 2 if ob["shape"] == "box" else size[0] / 2
    xs, ys = [], []
    for cx in (px - rad, px + rad):
        for cz in (pz - rad, pz + rad):
            for cy in (0.0, size[1]):
                v = sub([cx, cy, cz], view["position"])
                z = dot(v, view["forward"])
                if z < 0.05:  # 꼭짓점이 카메라 뒤에 있으면 화면 전체를 본다
                    return 0, w - 1, 0, h - 1
                xs.append(dot(v, view["right"]) / (z * view["tan_h"]))
                ys.append(dot(v, view["up"]) / (z * view["tan_v"]))
    i0, i1 = (min(xs) + 1) / 2 * w - 1, (max(xs) + 1) / 2 * w + 1
    j0, j1 = (1 - max(ys)) / 2 * h - 1, (1 - min(ys)) / 2 * h + 1
    return max(0, math.floor(i0)), min(w - 1, math.ceil(i1)), max(0, math.floor(j0)), min(h - 1, math.ceil(j1))


def _grid_rays(rects: list[tuple[int, int, int, int]], view: dict):
    """화면 사각형 rects 중 하나에라도 드는 광선 격자 칸마다 (광선 방향, 그 칸이 드는 물체 번호들)을 낸다.

    칸은 행(j) → 열(i) 순서로, 물체 번호는 오름차순으로 낸다.
    """
    w, h = VIS_GRID
    f, r, u = view["forward"], view["right"], view["up"]
    rx, ry, rz = (x * view["tan_h"] for x in r)
    ux, uy, uz = (x * view["tan_v"] for x in u)
    for j in range(min(rc[2] for rc in rects), max(rc[3] for rc in rects) + 1):
        ny = 1 - (j + 0.5) * 2 / h
        rows = [k for k, rc in enumerate(rects) if rc[2] <= j <= rc[3]]
        if not rows:
            continue
        for i in range(min(rects[k][0] for k in rows), max(rects[k][1] for k in rows) + 1):
            nx = (i + 0.5) * 2 / w - 1
            d = [f[0] + nx * rx + ny * ux, f[1] + nx * ry + ny * uy, f[2] + nx * rz + ny * uz]
            yield d, [k for k in rows if rects[k][0] <= i <= rects[k][1]]


def _ray_hits(cam: list, d: list, ks: list[int], objects: list[dict], hits: list) -> tuple[list[int], int]:
    """광선 cam + t·d가 물체 ks 중 닿는 물체 번호들과 그중 가장 가까운 물체 번호 (아무것도 안 닿으면 -1).

    거리가 같으면 번호가 작은 물체를 가장 가깝다고 본다.
    """
    hit, nearest, best = [], -1, math.inf
    for k in ks:
        t = hits[k](cam, d, objects[k])
        if t is not None:
            hit.append(k)
            if t < best:
                nearest, best = k, t
    return hit, nearest


def visible_fractions(objects: list[dict], view: dict) -> list[float]:
    """물체마다 입력 이미지 안의 실루엣 중 다른 물체에 가려지지 않은 비율 (0~1).

    view는 카메라 위치와 (롤을 반영한) forward·right·up, 수평·수직 tan(fov/2)이다.
    광선 격자의 칸마다 그 칸을 지나는 물체와 가장 가까운 물체를 세어 보이는 칸 ÷ 지나는 칸을 낸다.
    """
    n = len(objects)
    if n <= 1:
        return [1.0] * n
    rects = [_screen_rect(ob, view) for ob in objects]
    hits = [_hit_box if ob["shape"] == "box" else _hit_cylinder for ob in objects]
    covered, front = [0] * n, [0] * n
    for d, ks in _grid_rays(rects, view):
        hit, nearest = _ray_hits(view["position"], d, ks, objects, hits)
        for k in hit:
            covered[k] += 1
        if nearest >= 0:
            front[nearest] += 1
    return [front[k] / covered[k] if covered[k] else 0.0 for k in range(n)]


def _place_objects(rng: random.Random, W: float, D: float, cam: list, visible, view: dict) -> list[dict]:
    """서로 겹치지 않고 카메라에 보이며, 다른 물체에 대부분 가려지지 않는 위치에 물체 1~4개를 놓는다.

    새 물체를 놓으면 새 물체와 이미 놓인 물체 모두 실루엣의 MIN_VISIBLE_FRACTION 이상이 보여야 한다.
    가림 판정은 난수를 쓰지 않으므로, 가림 때문에 거부되는 배치가 없는 시드는 이전과 같은 장면이 된다.
    """
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
            candidate = {"shape": shape, "size": size, "position": [x, z],
                         "yaw_deg": rng.uniform(0, 90) if shape == "box" else 0.0,
                         "color": colors[len(objects)], "_r": radius}
            if min(visible_fractions(objects + [candidate], view)) < MIN_VISIBLE_FRACTION:
                continue
            objects.append(candidate)
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

    # 가림 판정용 시점. three.js처럼 lookAt 뒤에 카메라 z축으로 roll만큼 돌린다
    a = math.radians(roll)
    view = {"position": cam, "forward": forward, "tan_h": tan_h, "tan_v": tan_v,
            "right": [math.cos(a) * r + math.sin(a) * q for r, q in zip(right, up)],
            "up": [-math.sin(a) * r + math.cos(a) * q for r, q in zip(right, up)]}
    objects = _place_objects(rng, W, D, cam, visible, view)
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
