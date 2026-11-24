"""사진에서 관측 찾기: Codex 벽 선 둘레의 실제 경계, 그 교점에서 찾는 벽 모서리 세로선, 물체 옆선(연직선)."""

from __future__ import annotations

import numpy as np

from ..edges import EdgeImage, Segment, clip_segment, snap
from .boxes import padded
from .model import RoomModel
from .samples import codex_lines

END_MARGIN = 6.0              # 선분 양 끝(모서리 만나는 곳)은 이만큼 빼고 잰다
LINE_RADIUS = 22.0            # Codex 벽 선은 둘레 이만큼 안에서 경계를 찾는다
CORNER_RADIUS = 14.0          # 벽 모서리 세로선은 바닥선(·윗변) 교점 둘레 이만큼에서 찾는다
VERT_RADIUS = 8.0             # 물체 옆선은 bbox 옆변에서 이만큼 안에서 찾는다
VERT_WIDEN = 1.5              # 거기서 못 찾으면 이 배만큼 넓혀 한 번 더 찾는다. Codex bbox 옆변은 실루엣에서 10픽셀쯤
                              # 벗어나기도 해서, 그 1픽셀 차이로 옆선이 요청마다 잡히기도 하고 안 잡히기도 했다
VERT_TRIM = 0.18              # 물체 위아래(원기둥 뚜껑·상자 윗면)는 이 비율만큼 빼고 잰다
MIN_CONTRAST = 2.0            # 둘레보다 이만큼 뚜렷해야 경계로 본다
MIN_SUPPORT = 0.2             # 잰 칸 가운데 이만큼에서 경계점이 잡혀야 쓴다
MIN_LENGTH = 24.0             # 이보다 짧게 잡힌 경계는 쓰지 않는다 (픽셀)
CORNER_CONTRAST_CAP = 20.0    # 모서리 후보를 견줄 때 대비는 이만큼에서 자른다 (뚜렷한 것끼리는 길이로 가린다)


def accepted(s: Segment | None, min_support: float = MIN_SUPPORT, min_length: float = MIN_LENGTH) -> bool:
    """찾은 경계를 관측으로 쓸지: 둘레보다 뚜렷하고, 충분히 잡혔고, 충분히 긴지."""
    return s is not None and s.contrast >= MIN_CONTRAST and s.support >= min_support and s.length >= min_length


def clip_inset(p0, p1, W: int, H: int):
    """선분을 화면 안으로 자르고 양 끝 END_MARGIN을 뺀다. 화면 밖이거나 너무 짧으면 None."""
    c = clip_segment(p0, p1, W, H)
    if c is None:
        return None
    L = float(np.linalg.norm(c[1] - c[0]))
    if L < 2 * END_MARGIN + 16:
        return None
    d = (c[1] - c[0]) / L
    return c[0] + END_MARGIN * d, c[1] - END_MARGIN * d


def snap_codex_lines(E: EdgeImage, analyses: list[dict], boxes: list, radius: float = LINE_RADIUS) -> dict:
    """표본마다 Codex 벽 선 둘레에서 사진의 경계를 찾고, 모서리마다 가장 길게 잡힌 것을 고른다."""
    found: dict[str, list[Segment]] = {}
    for a in analyses:
        for k, (p0, p1) in codex_lines(a).items():
            ends = clip_inset(p0, p1, E.W, E.H)
            if ends is None:
                continue
            seg = snap(E, ends[0], ends[1], radius, boxes)
            if accepted(seg):
                found.setdefault(k, []).append(seg)
    return {k: max(v, key=lambda sg: sg.support * sg.length) for k, v in found.items()}


def _intersect(l1, l2):
    """두 직선 (n, c)의 교점. 거의 나란하면 None."""
    A = np.array([l1[0], l2[0]])
    if abs(np.linalg.det(A)) < 1e-6:
        return None
    return np.linalg.solve(A, np.array([l1[1], l2[1]]))


def _line_of(seg: Segment | None, codex: tuple | None):
    if seg is not None:
        return seg.normal, seg.c
    if codex is not None:
        p0, p1 = codex
        d = p1 - p0
        n = np.array([-d[1], d[0]]) / max(np.linalg.norm(d), 1e-9)
        return n, float(n @ p0)
    return None


def _crossing(first: dict, lines: dict, a: str, b: str):
    """모서리 a와 b의 직선(찾은 경계, 없으면 Codex 선) 교점. 직선이 없거나 거의 나란하면 None."""
    la = _line_of(first.get(a), lines.get(a))
    lb = _line_of(first.get(b), lines.get(b))
    return _intersect(la, lb) if la and lb else None


def _extend_vertically(model: RoomModel, x: np.ndarray, pb, pt):
    """교점이 하나뿐일 때 (아래 끝 pb, 위 끝 pt) 중 없는 쪽을 연직 방향으로 화면 밖까지 늘린다. 방향을 모르면 None."""
    anchor = pb if pb is not None else pt
    ln = model.vertical_line(x, anchor)
    if ln is None:
        return None
    t = np.array([ln[0][1], -ln[0][0]])
    t = t if t[1] < 0 else -t                    # 위쪽 방향
    if pb is not None:
        return pb, pb + t * (2.0 * model.H)
    return pt - t * (2.0 * model.H), pt


def _corner_ends(model: RoomModel, x: np.ndarray, side: str, first: dict, lines: dict):
    """뒷벽과 side 벽 모서리 세로선의 예상 (아래 끝, 위 끝): 바닥선 교점과 윗변 교점. 둘 다 없으면 None."""
    pb = _crossing(first, lines, "back_bottom", f"{side}_bottom")
    pt = _crossing(first, lines, "back_top", f"{side}_top")
    if pb is None and pt is None:
        return None
    if pb is None or pt is None:
        return _extend_vertically(model, x, pb, pt)
    return pb, pt


def _corner_guesses(model: RoomModel, x: np.ndarray, side: str, first: dict, lines: dict, analyses: list[dict]) -> list:
    """모서리 세로선의 예상 위치 후보들: 찾은 경계(없으면 Codex 선 중앙값)의 교점, Codex 선 중앙값만의 교점,
    표본마다 Codex 선의 교점. 찾은 바닥선 하나가 다른 경계(물체 윗면, 그림자)를 잡았으면 첫 후보는 엉뚱한 곳을 가리킨다."""
    out = []
    for f, ln in [(first, lines), ({}, lines)] + [({}, codex_lines(a)) for a in analyses]:
        ends = _corner_ends(model, x, side, f, ln)
        ends = None if ends is None else clip_inset(ends[0], ends[1], model.W, model.H)
        if ends is not None and all(max(np.linalg.norm(ends[0] - e[0]), np.linalg.norm(ends[1] - e[1])) > 4.0
                                    for e in out):
            out.append(ends)
    return out


def _corner_quality(s: Segment) -> float:
    """모서리 후보 선분의 좋음: 잡힌 길이 × 대비(포화)."""
    return s.support * s.length * min(s.contrast, CORNER_CONTRAST_CAP)


def find_corners(E: EdgeImage, model: RoomModel, x: np.ndarray, first: dict, lines: dict, boxes: list,
                 analyses: list[dict] = ()) -> dict:
    """뒷벽과 옆벽이 만나는 세로 모서리: 예상 위치 후보(_corner_guesses)마다 바닥선 교점에서 윗변 교점(없으면 연직
    방향)까지를 둘레 CORNER_RADIUS 안에서 찾고, 가장 뚜렷하게 길게 잡힌 것을 쓴다."""
    out = {}
    for side in ("left", "right"):
        name = f"corner_{side}"
        if name not in model.edges():
            continue
        segs = [snap(E, e[0], e[1], CORNER_RADIUS, boxes, max_tilt=12.0)
                for e in _corner_guesses(model, x, side, first, lines, analyses)]
        segs = [s for s in segs if accepted(s)]
        if segs:
            out[name] = max(segs, key=_corner_quality)
    return out


def _side_anchor(model: RoomModel, x: np.ndarray, box: tuple, left: bool) -> np.ndarray | None:
    """옆선이 bbox 옆변에 닿는 점. 연직선이 화면에서 기울어 있으면(롤, 피치) 옆선은 bbox 옆변에 한쪽 끝(위나 아래)에서만
    닿으므로, 그 끝 모서리를 돌려준다. 방향을 모르면 None."""
    L, T, R, B = box
    u = L if left else R
    ln = model.vertical_line(x, np.array([u, (T + B) / 2]))
    if ln is None:
        return None
    n, _ = ln
    t = np.array([n[1], -n[0]])
    if abs(t[1]) < 1e-6:
        return None
    lean = t[0] / t[1]                      # 아래로 1픽셀 갈 때 옆으로 가는 양
    at_top = (lean > 0) == left             # 왼쪽 옆선은 x가 가장 작은 끝, 오른쪽 옆선은 가장 큰 끝에서 닿는다
    return np.array([u, T if at_top else B])


def _object_side(E: EdgeImage, model: RoomModel, x: np.ndarray, anchor: np.ndarray, y0: float, y1: float,
                 radius: float, nearer: list) -> Segment | None:
    """옆선이 닿는 점 anchor를 지나는 연직선의 y0~y1 구간 둘레에서 찾은 경계."""
    ln = model.vertical_line(x, anchor)
    if ln is None:
        return None
    n, _ = ln
    t = np.array([n[1], -n[0]])
    if abs(t[1]) < 0.5:
        return None
    m = anchor + t * (((y0 + y1) / 2 - anchor[1]) / t[1])
    half = (y1 - y0) / 2 / abs(t[1])
    return snap(E, m - half * t, m + half * t, radius, nearer, max_tilt=6.0)


def object_verticals(E: EdgeImage, model: RoomModel, x: np.ndarray, objects: list, radius: float) -> list:
    """물체(원기둥 실루엣, 상자 모서리)의 왼쪽·오른쪽 옆선. 3D에서 연직이므로 연직 소실점을 지난다.
    가까운 물체(bbox 아래가 더 낮은 것)에 가린 곳은 재지 않는다. 화면 가장자리에 붙은 옆변은 쓰지 않는다."""
    out = []
    for i, box in enumerate(objects):
        L, T, R, B = box
        h = B - T
        if h < 40 or R - L < 8:
            continue
        nearer = [padded(o) for j, o in enumerate(objects) if j != i and o[3] > B + 2]
        y0, y1 = T + VERT_TRIM * h, B - VERT_TRIM * h
        for left, u in ((True, L), (False, R)):
            if u < 4 or u > model.W - 4:
                continue
            anchor = _side_anchor(model, x, box, left)
            if anchor is None:
                continue
            for r in (radius, radius * VERT_WIDEN):
                s = _object_side(E, model, x, anchor, y0, y1, r, nearer)
                if accepted(s, min_support=0.5, min_length=0.4 * h):
                    out.append(s)
                    break
    return out


def first_observations(E: EdgeImage, model: RoomModel, x: np.ndarray, analyses: list[dict], lines: dict,
                       boxes: list, objects: list) -> dict:
    """모델과 무관하게 한 번 정하는 관측: Codex 벽 선 둘레의 경계, 그 교점 둘레의 벽 모서리 세로선, 물체 옆선.
    x(표본 초기값 중앙값)는 연직 방향을 잴 때만 쓴다."""
    first = {k: v for k, v in snap_codex_lines(E, analyses, boxes).items() if k in model.edges()}
    first.update(find_corners(E, model, x, first, lines, boxes, analyses))
    first.update({f"vertical_{i}": sg for i, sg in enumerate(object_verticals(E, model, x, objects, VERT_RADIUS))})
    return first
