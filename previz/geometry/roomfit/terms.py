"""벽 경계 밖의 카메라 관측 항: 사진에서 잰 원기둥 윗면 타원, 상자 모서리 선분(boxlines.py), 표본 지평선을
x → 잔차 함수로 만든다."""

from __future__ import annotations

import math

import numpy as np

from ..camera import horizon_row
from ..edges import EdgeImage
from ..ellipses import measure_top, residuals
from .boxes import consensus_boxes, consensus_cylinders, occluders
from .boxlines import box_term
from .model import bounded_focal_px, camera_up, focal_px

ELLIPSE_CAUCHY = 3.0          # 타원 잔차(σ 단위)의 견고 무게 폭: 잘못 잰 타원 하나가 답을 끌지 못하게
SIG_HORIZON_PX = 20.0         # 표본들이 잰 지평선(화면 가운데 열 높이) 중앙값의 폭 (픽셀). 표본끼리 함께 틀리기도 해서 넓게 둔다


def cylinder_tops(E: EdgeImage, analyses: list[dict], x: np.ndarray) -> list:
    """표본 과반이 원기둥이라고 한 물체마다 사진에서 잰 윗면 타원 (previz/geometry/ellipses.py)."""
    up = camera_up(x)
    f = focal_px(E.H, x[0])
    boxes = consensus_boxes(analyses)
    tops = []
    for b in consensus_cylinders(analyses):
        e = measure_top(E, b, occluders(b, boxes, E.H), f, up, math.radians(x[2]))
        if e is not None:
            tops.append(e)
    return tops


def ellipse_term(tops: list, W: int, H: int):
    """x → 타원 잔차 (법선과 연직의 차 ÷ σ, 타원마다 Cauchy로 줄인 두 성분)."""
    def extra(x):
        up = camera_up(x)
        f = bounded_focal_px(H, x[0])
        r = residuals(tops, f, W / 2, H / 2, up).reshape(-1, 2)
        n = np.linalg.norm(r, axis=1, keepdims=True)
        rho = ELLIPSE_CAUCHY ** 2 * np.log1p((n / ELLIPSE_CAUCHY) ** 2)
        return (r * np.sqrt(rho) / np.maximum(n, 1e-12)).ravel()
    return extra


def horizon_term(v0: float, H: int):
    """x → (지평선이 화면 가운데 열을 지나는 높이 − v0) ÷ SIG_HORIZON_PX."""
    def extra(x):
        vh = horizon_row(H, bounded_focal_px(H, x[0]), x[1], x[2])
        return np.array([(vh - v0) / SIG_HORIZON_PX])
    return extra


def camera_terms(tops: list, v0: float | None, W: int, H: int, box_groups: list | None = None):
    """타원 항, 상자 선분 항, 지평선 항(있는 것만)을 이어 붙인 x → 잔차 함수. 모두 없으면 None."""
    fns = [f for f in (ellipse_term(tops, W, H) if tops else None,
                       box_term(box_groups, W, H) if box_groups else None,
                       horizon_term(v0, H) if v0 is not None else None) if f is not None]
    if not fns:
        return None
    return lambda x: np.concatenate([f(x) for f in fns])
