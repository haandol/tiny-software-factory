"""채점: 정답 측정값과 제품 측정값을 지표로 바꾸고 충실도 점수를 낸다 (CONTRACT.md 6절)."""

from __future__ import annotations

import itertools
import math

from .policy import WORST_METRICS
from .scenes import cross, dot, norm, sub


def _hex_to_lab(hex_color: str) -> tuple[float, float, float]:
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    x = (lin[0] * 0.4124 + lin[1] * 0.3576 + lin[2] * 0.1805) / 0.95047
    y = lin[0] * 0.2126 + lin[1] * 0.7152 + lin[2] * 0.0722
    z = (lin[0] * 0.0193 + lin[1] * 0.1192 + lin[2] * 0.9505) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


def delta_e(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 100.0
    return math.dist(_hex_to_lab(a), _hex_to_lab(b))


def camera_frame(an: dict):
    """카메라 바로 아래 바닥점을 원점으로, 카메라가 보는 수평 방향을 +z로 하고, 카메라 높이로 나눈 좌표계."""
    cam = an["camera"]
    p, f = cam["position"], cam["forward"]
    ground_y = an["groundY"] if an["groundY"] is not None else 0.0
    height = p[1] - ground_y
    if height <= 1e-6:
        return None
    fh = norm([f[0], 0.0, f[2]])
    xr = cross(fh, [0.0, 1.0, 0.0])
    origin = [p[0], ground_y, p[2]]

    def to_local(point):
        v = sub(point, origin)
        return [dot(v, xr) / height, v[1] / height, dot(v, fh) / height]
    return {"to_local": to_local, "height": height}


def angles(an: dict) -> tuple[float, float]:
    cam = an["camera"]
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, cam["forward"][1]))))
    roll = math.degrees(math.asin(max(-1.0, min(1.0, cam["right"][1]))))
    return pitch, roll


def iou(a: str, b: str) -> float:
    ia, ib = int(a, 2), int(b, 2)
    union = (ia | ib).bit_count()
    return (ia & ib).bit_count() / union if union else 1.0


def score_metrics(m: dict) -> float:
    """충실도 = 100 × F1 × (0.20·화각 + 0.15·기울기 + 0.20·배치 + 0.15·(크기·색) + 0.30·IoU)."""
    def clamp(x):
        return max(0.0, min(1.0, x))
    s_fov = clamp(1 - m["camera.fov_error_deg"] / 30)
    s_pose = clamp(1 - (m["camera.pitch_error_deg"] + m["camera.roll_error_deg"]) / 2 / 15)
    s_pos = clamp(1 - m["objects.position_error"] / 0.5)
    s_sc = (clamp(1 - m["objects.size_error"]) + clamp(1 - m["objects.color_delta_e"] / 60)) / 2
    s_iou = clamp(m["reprojection.iou"])
    weighted = 0.20 * s_fov + 0.15 * s_pose + 0.20 * s_pos + 0.15 * s_sc + 0.30 * s_iou
    return round(100 * m["objects.f1"] * weighted, 2)


def _match(g_objs: list[dict], p_objs: list[dict]) -> list[tuple[int, int]]:
    """정답 물체와 후보 물체를 카메라 높이 1배 이내에서, 짝 수가 가장 많고 거리 합이 가장 작게 짝짓는다."""
    if len(p_objs) > 12:  # 정답 물체에 가장 가까운 후보 12개만 본다
        p_objs[:] = sorted(p_objs, key=lambda p: min(math.dist(p["c"], g["c"]) for g in g_objs))[:12]
    best, best_cost = [], float("inf")
    k = min(len(g_objs), len(p_objs))
    for g_sel in itertools.combinations(range(len(g_objs)), k):
        for p_sel in itertools.permutations(range(len(p_objs)), k):
            pairs = [(gi, pi) for gi, pi in zip(g_sel, p_sel) if math.dist(g_objs[gi]["c"], p_objs[pi]["c"]) < 1.0]
            cost = sum(math.dist(g_objs[gi]["c"], p_objs[pi]["c"]) for gi, pi in pairs) - 10 * len(pairs)
            if cost < best_cost:
                best, best_cost = pairs, cost
    return best


def _object_metrics(g_objs, p_objs, pairs, room_diag) -> dict:
    pos_err, size_err, color_err = [], [], []
    for gi, pi in pairs:
        g, p = g_objs[gi], p_objs[pi]
        pos_err.append(math.hypot(g["c"][0] - p["c"][0], g["c"][2] - p["c"][2]) / room_diag)
        gh, ph = sorted([g["s"][0], g["s"][2]]), sorted([p["s"][0], p["s"][2]])
        rel = [abs(ph[i] - gh[i]) / gh[i] for i in range(2)] + [abs(p["s"][1] - g["s"][1]) / g["s"][1]]
        size_err.append(sum(rel) / 3)
        color_err.append(delta_e(g["color"], p["color"]))
    n = len(pairs)
    return {"objects.position_error": sum(pos_err) / n, "objects.size_error": sum(size_err) / n,
            "objects.color_delta_e": sum(color_err) / n}


def compare(gt: dict, pred: dict, spec: dict) -> dict:
    """정답 측정값(gt)과 제품 측정값(pred)을 지표로 바꾼다."""
    m = dict(WORST_METRICS)
    g_pitch, g_roll = angles(gt)
    p_pitch, p_roll = angles(pred)
    m["camera.fov_error_deg"] = abs(pred["camera"]["fov"] - gt["camera"]["fov"])
    m["camera.pitch_error_deg"] = abs(p_pitch - g_pitch)
    m["camera.roll_error_deg"] = abs(p_roll - g_roll)
    m["reprojection.iou"] = iou(gt["mask"], pred["mask"])

    gf, pf = camera_frame(gt), camera_frame(pred)
    if gf and pf:
        def local(objs, frame):
            return [{"c": frame["to_local"](o["center"]), "s": [x / frame["height"] for x in o["size"]],
                     "color": o["color"]} for o in objs]
        g_objs, p_objs = local(gt["objects"], gf), local(pred["objects"], pf)
        pairs = _match(g_objs, p_objs)
        m["objects.f1"] = 2 * len(pairs) / (len(g_objs) + len(p_objs)) if (g_objs or p_objs) else 1.0
        if pairs:
            room_diag = math.hypot(spec["room"]["width"], spec["room"]["depth"]) / gf["height"]
            m.update(_object_metrics(g_objs, p_objs, pairs, room_diag))
    return {k: round(v, 4) for k, v in m.items()}
