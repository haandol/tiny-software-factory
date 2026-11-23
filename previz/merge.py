"""표본 합치기: 같은 사진을 여러 번 잰 Codex 측정값의 물체를 하나로 묶는다.

표본 하나의 물체 측정(bbox 변, 바닥 모서리, 크기, 색)은 잴 때마다 몇 픽셀·몇 %씩 흔들리고, 그 흔들림이 물체 배치와
크기를 바꾼다 (같은 장면을 다시 생성하면 충실도가 3점 넘게 달라졌다). 그래서 장면의 물체는 표본 하나에서 가져오지 않는다.
1) 표본들의 물체를 bbox 겹침으로 짝짓는다 (previz/geometry/roomfit/boxes.py의 match_objects). 표본 과반이 찾은 물체만 쓴다.
2) 물체마다 합친 측정값을 만든다: bbox 변·크기·색은 중앙값, 모양은 다수결, 바닥 모서리는 같은 개수를 잰 표본들의
   점별 중앙값. 크기는 표본마다 자기 카메라 높이로 나눠 견준다 (장면 축척은 카메라 높이로 정해진다).
3) 합친 측정값으로 맞춘 물체와 표본마다 맞춘 물체를 카메라 높이 단위로 바꿔, 위치·크기·방향·색을 변수마다 중앙값으로 묶는다.
   bbox가 화면에 잘린 물체는 맞추기가 여러 해 사이를 오가므로, 해 하나보다 해들의 중앙값이 덜 흔들린다.
"""

from __future__ import annotations

import math
import os
from collections import Counter

import numpy as np

from .geometry.camera import Cam
from .geometry.objects import fit_object
from .geometry.roomfit.boxes import match_objects, sized_box
from .geometry.values import clamp, points, to_float, to_hex

SIZE_KEYS = ("width_m", "depth_m", "height_m")
MAX_SPREAD = float(os.environ.get("PREVIZ_MERGE_SPREAD", "0.3"))   # 합친 맞춤이 표본별 맞춤 중앙값에서 이만큼(카메라 높이 단위) 넘게 벗어나면 쓰지 않는다


def camera_height(a: dict) -> float:
    return clamp(to_float((a.get("camera") or {}).get("height_m"), 1.5), 0.3, 5.0)


def _shape(o: dict) -> str:
    return "cylinder" if str(o.get("shape", "")).lower().startswith("cyl") else "box"


def _hex_rgb(c: str) -> list[int]:
    return [int(c[i:i + 2], 16) for i in (1, 3, 5)]


def median_color(colors: list) -> str:
    rgb = [_hex_rgb(c) for c in (to_hex(x, "") for x in colors) if c]
    if not rgb:
        return "#999999"
    return "#" + "".join(f"{int(round(v)):02x}" for v in np.median(np.array(rgb, float), axis=0))


def _median_corners(objs: list[dict]) -> list[dict]:
    """같은 개수(가장 많은 표본이 잰 개수, 같으면 더 많은 쪽)의 바닥 모서리를 잰 표본들의 점별 중앙값 (왼쪽부터)."""
    sets = [sorted(points(o.get("bottom_corners"))) for o in objs]
    sets = [s for s in sets if s]
    if not sets:
        return []
    counts = Counter(len(s) for s in sets)
    k = max(counts, key=lambda n: (counts[n], n))
    med = np.median(np.array([s for s in sets if len(s) == k], float), axis=0)
    return [{"x": float(u), "y": float(v)} for u, v in med]


def majority_shape(shapes: list[str], tie: str) -> str:
    c = Counter(shapes)
    if c["box"] == c["cylinder"]:
        return tie
    return c.most_common(1)[0][0]


def merge_measurements(objs: list[dict], scales: list[float], height: float) -> dict:
    """한 물체를 잰 표본들의 측정값(objs, 표본 카메라 높이 scales)을 카메라 높이 height의 측정값 하나로."""
    boxes = np.array([sized_box(o) for o in objs], float)
    med = np.median(boxes, axis=0)
    near = int(np.argmin(np.abs(boxes - med).sum(1)))      # 모양이 반반이면 bbox가 중앙값에 가장 가까운 표본을 따른다
    shape = majority_shape([_shape(o) for o in objs], _shape(objs[near]))
    same = [o for o in objs if _shape(o) == shape]
    out = {"shape": shape, "color": median_color([o.get("color") for o in objs]),
           "bbox": dict(zip(("left", "top", "right", "bottom"), (float(v) for v in med))),
           "bottom_corners": _median_corners(same) if shape == "box" else []}
    for k in SIZE_KEYS:
        rel = [to_float(o.get(k), math.nan) / s for o, s in zip(objs, scales)]
        rel = [v for v in rel if math.isfinite(v) and v > 0]
        out[k] = float(np.median(rel)) * height if rel else None
    return out


def _position(fit: dict, h: float) -> np.ndarray:
    return np.array(fit["position"], float) / h


def _scaled(fit: dict, k: float) -> dict:
    """맞춘 물체를 카메라 높이 비 k만큼 늘린 사본 (방향·색은 그대로)."""
    return {**fit, "position": [v * k for v in fit["position"]], "size": [v * k for v in fit["size"]]}


def robust_fit(merged: dict | None, fits: list[dict], heights: list[float], h: float) -> dict | None:
    """한 물체의 최종 맞춤 (카메라 높이 h 단위). 합친 측정값으로 맞춘 merged가 표본별 맞춤 위치의 중앙값에서
    MAX_SPREAD 안이면 merged를 쓴다. 아니면(합친 측정값이 맞추기의 다른 해에 빠졌다) 표본별 맞춤 가운데 위치가
    중앙값에 가장 가까운 것을 쓴다."""
    if not fits:
        return merged
    pos = np.array([_position(f, fh) for f, fh in zip(fits, heights)])
    med = np.median(pos, axis=0)
    if merged is not None and float(np.linalg.norm(_position(merged, h) - med)) <= MAX_SPREAD:
        return merged
    i = int(np.argmin(np.linalg.norm(pos - med, axis=1)))
    return _scaled(fits[i], h / heights[i])


def merged_objects(analyses: list[dict], fitted: list[list[dict] | None], cam: Cam) -> list[dict]:
    """표본 과반이 찾은 물체마다 하나 (가까운 것부터), 카메라 cam(높이 cam.h)의 장면 단위로.
    fitted[i]는 표본 i의 물체를 그 표본의 카메라 높이로 맞춘 결과(각 물체의 "index"는 측정값 물체 번호), 못 풀었으면 None."""
    heights = [camera_height(a) for a in analyses]
    out = []
    for members in match_objects(analyses):
        objs = [analyses[si]["objects"][oi] for si, oi in members]
        merged = fit_object(merge_measurements(objs, [heights[si] for si, _ in members], cam.h), cam)
        fits, fh = [], []
        for si, oi in members:
            f = next((f for f in fitted[si] or [] if f.get("index") == oi), None)
            if f:
                fits.append(f)
                fh.append(heights[si])
        fit = robust_fit(merged, fits, fh, cam.h)
        if fit:
            out.append(fit)
    return out


def merged_measurements_all(analyses: list[dict]) -> list[dict]:
    heights = [camera_height(a) for a in analyses]
    h = float(np.median(heights)) if heights else 1.5
    out = []
    for members in match_objects(analyses):
        objs = [analyses[si]["objects"][oi] for si, oi in members]
        out.append(merge_measurements(objs, [heights[si] for si, _ in members], h))
    return out
