"""벽 경계로 카메라·방 풀기: 화각 격자 시작값마다 모델을 맞추고 다듬어, 사진의 경계를 가장 길게 설명하는 해를 고른다."""

from __future__ import annotations

import numpy as np

from ..edges import EdgeImage
from .boxes import consensus_boxes, object_boxes
from .fit import FIT_ERRORS, FitResult, Prior, evaluate, fit_points, fit_to_codex, prune_lines, with_fov
from .model import RoomModel
from .observe import first_observations
from .samples import pooled_codex_lines, prior_center, prior_from, vote_walls

FOV_GRID = tuple(range(24, 115, 6))     # 화각 시작값 격자 (도)
GRID_HOLD_SIG = 1.0                       # 시작값마다 Codex 선으로 먼저 맞출 때 화각을 묶는 폭 (도)
STRONG_CONTRAST = 8.0                     # 이만큼 뚜렷한 경계는 다른 경계를 잘못 잡았을 가능성이 낮다
REFINE_TOP = 3                            # 격자 맞추기에서 경계를 가장 길게 설명한 이만큼의 해를 다듬는다


def calibrate(analyses: list[dict], W: int, H: int, img: np.ndarray, E: EdgeImage | None = None,
              log=None) -> FitResult | None:
    """사진 경계로 카메라와 방을 푼다.

    1) 관측을 모델과 무관하게 한 번 정한다: Codex 벽 선 둘레(±22픽셀)의 실제 경계, 그 선들의 교점 둘레의 벽 모서리
       세로선, 표본 과반이 찾은 물체의 옆선.
    2) 그 관측에 화각 격자의 시작값마다 모델을 맞추고(나머지 변수는 Codex 선으로 먼저 맞춘다), 잘 맞은 해들은
       벽 선을 하나씩 빼고도 맞춰 본다 (다른 경계를 잡은 선 하나를 걸러낸다).
    3) 관측된 경계를 1.5픽셀 안에서 가장 길게 설명하는 해를 고른다.
    """
    walls, has_top = vote_walls(analyses)
    if not walls:
        return None
    model = RoomModel(W, H, walls, has_top)
    E = E or EdgeImage(img)
    boxes = object_boxes(analyses)
    objects = consensus_boxes(analyses)
    med = prior_center(analyses, W, H)
    if med is None:
        return None
    prior = prior_from(med)
    lines = pooled_codex_lines(analyses)
    first = first_observations(E, model, med, analyses, lines, boxes, objects)
    if not any(not k.startswith("vertical") for k in first):
        return None
    best = _search(model, med, lines, first, prior)
    if not best:
        return None
    if log:
        _log_best(log, best, first)
    fov, x, sup = best[0]
    rms, _ = evaluate(model, x, first)
    return FitResult(x, model, first, rms, sup)


def _search(model: RoomModel, med: np.ndarray, lines: dict, first: dict, prior: Prior) -> list:
    """격자 맞추기 → 뚜렷한 경계로 다시 맞추기 → 선 빼 보기를 거친 모든 해 가운데 가장 좋은 REFINE_TOP개."""
    sols = _grid_fits(model, med, lines, first, prior)
    sols += _strong_refits(model, _top(model, sols, first), first, prior)
    sols += _pruned_fits(model, _top(model, sols, first), first, prior)
    return _top(model, sols, first)


def _grid_fits(model: RoomModel, med: np.ndarray, lines: dict, first: dict, prior: Prior) -> list:
    """화각 격자의 시작값마다 (화각만 묶고 Codex 선으로 먼저 맞춘 뒤) 관측에 맞춘 (시작 화각, 해)."""
    out = []
    for fov in FOV_GRID:
        hold = prior.held(fov, GRID_HOLD_SIG)
        try:
            x = fit_to_codex(model, with_fov(med, fov), lines, hold)
            x = fit_points(model, x, first, prior)
        except FIT_ERRORS:
            continue
        out.append((fov, x))
    return out


def _strong_refits(model: RoomModel, top: list, first: dict, prior: Prior) -> list:
    """뚜렷한 경계가 셋 이상이고 흐린 경계도 있으면, 좋은 해마다 뚜렷한 경계만으로 먼저 맞춘 해와
    거기서 모든 관측으로 다시 맞춘 해."""
    strong = {k: v for k, v in first.items() if v.contrast >= STRONG_CONTRAST}
    if not 3 <= len(strong) < len(first):
        return []
    out = []
    for fov, x, _ in top:
        try:
            xs = fit_points(model, x, strong, prior)
            out += [(fov, xs), (fov, fit_points(model, xs, first, prior, warm=True))]
        except FIT_ERRORS:
            continue
    return out


def _pruned_fits(model: RoomModel, top: list, first: dict, prior: Prior) -> list:
    """좋은 해마다 선을 하나씩 빼 보며 거쳐 간 해 (previz/geometry/roomfit/fit.py의 prune_lines)."""
    out = []
    for fov, x, _ in top:
        out += [(fov, x2) for x2 in prune_lines(model, x, first, prior)]
    return out


def _top(model: RoomModel, sols: list, obs: dict) -> list:
    """가장 좋은 REFINE_TOP개 (시작 화각, 해, 설명한 경계 길이), 화각이 1° 넘게 다른 것끼리."""
    return _distinct(_rank(model, sols, obs), REFINE_TOP)


def _rank(model: RoomModel, sols: list, obs: dict) -> list:
    """(시작 화각, 해, 설명한 경계 길이)를 설명한 길이(20픽셀 단위) 큰 순, 같으면 잔차 작은 순으로."""
    out = []
    for fov, x in sols:
        rms, sup = evaluate(model, x, obs)
        out.append((round(sup / 20.0), -rms, fov, x, sup))
    out.sort(key=lambda c: (-c[0], -c[1]))
    return [(fov, x, sup) for _, _, fov, x, sup in out]


def _distinct(ranked: list, n: int) -> list:
    picked = []
    for c in ranked:
        if all(abs(c[1][0] - p[1][0]) > 1.0 for p in picked):
            picked.append(c)
        if len(picked) >= n:
            break
    return picked


def _log_best(log, best: list, first: dict) -> None:
    for fov, x, sup in best:
        log(f"  사진 맞추기 {fov}°→ fov {x[0]:.1f} pitch {x[1]:.1f} roll {x[2]:.1f} yaw {x[3]:.1f} "
            f"설명한 경계 {sup:.0f}px, 선 {sorted(first)}")
