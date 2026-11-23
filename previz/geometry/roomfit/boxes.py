"""표본들의 물체 bbox 묶기: 겹침으로 같은 물체를 짝짓고, 과반이 찾은 물체·원기둥의 bbox 중앙값과 가림 사각형을 낸다."""

from __future__ import annotations

import math

import numpy as np

OBJECT_PAD = 5.0              # 물체 bbox를 이만큼 넓혀 가린다


def read_box(o) -> tuple | None:
    """측정값 물체 하나의 bbox (L, T, R, B), 좌우·위아래를 정렬한 것. 읽을 수 없거나 유한하지 않으면 None."""
    b = o.get("bbox") if isinstance(o, dict) else None
    if not isinstance(b, dict):
        return None
    try:
        L, T, R, B = (float(b[k]) for k in ("left", "top", "right", "bottom"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (L, T, R, B)):
        return None
    L, R = sorted((L, R))
    T, B = sorted((T, B))
    return L, T, R, B


def sized_box(o) -> tuple | None:
    """read_box에서 가로·세로가 2픽셀 넘는 bbox만."""
    b = read_box(o)
    if b is None:
        return None
    L, T, R, B = b
    return b if R - L >= 2 and B - T >= 2 else None


def padded(box: tuple) -> tuple:
    """bbox를 OBJECT_PAD만큼 넓힌 가림 사각형."""
    L, T, R, B = box
    return (L - OBJECT_PAD, T - OBJECT_PAD, R + OBJECT_PAD, B + OBJECT_PAD)


def object_boxes(analyses: list[dict]) -> list[tuple]:
    """모든 표본의 물체 bbox (OBJECT_PAD만큼 넓힘). 벽 경계를 찾을 때 이 안은 보지 않는다."""
    boxes = []
    for a in analyses:
        for o in a.get("objects") or []:
            b = read_box(o)
            if b is not None:
                boxes.append(padded(b))
    return boxes


def _iou(a, b) -> float:
    iw = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = iw * ih
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _join_clusters(clusters: list[dict], si: int, objs: list, min_iou: float) -> set:
    """표본 si의 물체들을 겹침이 큰 쌍부터 묶음에 하나씩 넣는다 (묶음마다 표본 하나까지). 넣은 물체 번호들."""
    pairs = [(_iou(b, c["box"]), oi, ci, b) for oi, b in objs
             for ci, c in enumerate(clusters) if si not in c["samples"]]
    used_o, used_c = set(), set()
    for iou, oi, ci, b in sorted(pairs, key=lambda p: -p[0]):
        if iou < min_iou or oi in used_o or ci in used_c:
            continue
        used_o.add(oi)
        used_c.add(ci)
        c = clusters[ci]
        c["members"].append((si, oi))
        c["samples"].add(si)
        c["boxes"].append(b)
        c["box"] = tuple(np.median(np.array(c["boxes"]), axis=0))
    return used_o


def match_objects(analyses: list[dict], min_iou: float = 0.5) -> list[list[tuple[int, int]]]:
    """표본들의 물체를 bbox 겹침으로 짝지어 묶는다. 묶음 = [(표본 번호, 물체 번호)], 표본마다 하나까지.
    표본 과반에 나온 묶음만, bbox 중앙값이 가까운 것(아래쪽)부터 돌려준다."""
    clusters: list[dict] = []
    for si, a in enumerate(analyses):
        objs = [(oi, sized_box(o)) for oi, o in enumerate(a.get("objects") or [])]
        objs = [(oi, b) for oi, b in objs if b is not None]
        used = _join_clusters(clusters, si, objs, min_iou)
        clusters += [{"members": [(si, oi)], "samples": {si}, "boxes": [b], "box": b}
                     for oi, b in objs if oi not in used]
    n = len(analyses)
    keep = [c for c in clusters if 2 * len(c["samples"]) >= n]      # 표본 절반 이상 (n이 홀수면 과반)
    keep.sort(key=lambda c: -c["box"][3])
    return [c["members"] for c in keep]


def _median_box(analyses: list[dict], members: list) -> tuple:
    boxes = [sized_box(analyses[si]["objects"][oi]) for si, oi in members]
    return tuple(float(v) for v in np.median(np.array(boxes), axis=0))


def consensus_boxes(analyses: list[dict]) -> list[tuple]:
    """표본 과반이 찾은 물체마다 bbox 중앙값."""
    return [_median_box(analyses, members) for members in match_objects(analyses)]


def consensus_cylinders(analyses: list[dict]) -> list[tuple]:
    """표본 과반이 찾고, 그 과반이 원기둥이라고 한 물체의 bbox 중앙값."""
    out = []
    for members in match_objects(analyses):
        shapes = [str(analyses[si]["objects"][oi].get("shape", "")).startswith("cyl") for si, oi in members]
        if 2 * sum(shapes) > len(shapes):
            out.append(_median_box(analyses, members))
    return out


def occluders(box: tuple, others: list[tuple], H: int) -> list[tuple]:
    """box의 물체를 가릴 수 있는 다른 물체 bbox (OBJECT_PAD만큼 넓힘): 아래 끝이 더 낮거나, 둘 다 화면 아래에 잘려
    앞뒤를 모르는 것."""
    L, T, R, B = box
    out = []
    for o in others:
        if o == box:
            continue
        b = o[3]
        if b > B + 2 or (b >= H - 3 and B >= H - 3):
            out.append(padded(o))
    return out
