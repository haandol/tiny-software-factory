"""사진으로 카메라 맞추기의 전체 흐름: 벽 경계(없으면 물체 옆선) → 타원·상자 선분·지평선 더해 다시 맞추기 → 화각 프로파일."""

from __future__ import annotations

import numpy as np

from ..edges import EdgeImage
from .boxes import consensus_boxes
from .boxlines import box_lines
from .fit import FitResult, Prior, obs_line
from .fov import fov_interval, fov_profile, refit_with_tops
from .model import RoomModel
from .observe import VERT_RADIUS, object_verticals
from .samples import codex_horizon, sample_prior
from .terms import camera_terms, cylinder_tops
from .wallfit import calibrate

DETERMINED_DEG = 4.0          # 프로파일이 가려내지 못하는 화각 범위가 이보다 좁으면 사진이 화각을 정한 것이다
UNEXPLAINED_PX = 3.0          # 벽 경계 해의 모델 선에서 이만큼 안의 경계점을 그 해가 설명한 점으로 본다
MIN_EXPLAINED = 0.3           # 경계점 가운데 이만큼도 설명하지 못한 관측은 뺀다


def image_camera(analyses: list[dict], W: int, H: int, img: np.ndarray, log=None) -> dict | None:
    """사진 경계(벽 모서리, 물체 옆선)와 원기둥 윗면 타원으로 맞춘 카메라 {"vfov", "pitch", "roll", "basis", ...}.
    벽 경계를 하나도 잡지 못하면 물체 옆선·타원·표본 지평선으로 맞추고, 타원도 없으면 None."""
    E = EdgeImage(img)
    prior = sample_prior(analyses, W, H)
    fit = calibrate(analyses, W, H, img, E)
    objects_only = fit is None or not np.all(np.isfinite(fit.x))
    if not objects_only:
        fit = explained_only(fit)
    if objects_only:
        fit = object_fit(analyses, W, H, E, prior)
    tops = cylinder_tops(E, analyses, fit.x)
    groups = box_lines(E, analyses, fit.obs)
    v0 = codex_horizon(analyses) if objects_only else None      # 벽 경계가 있으면 지평선은 경계가 정한다
    if objects_only and not tops and not groups:
        return None                                   # 옆선만으로는 화각이 정해지지 않는다
    fit, prior = _refit_with_terms(fit, prior, tops, v0, W, H, groups)
    profile = fov_profile(fit, prior)
    lo, hi = fov_interval(profile)
    if log:
        _log_profile(log, profile, lo, hi, fit, tops, v0, groups)
    return _camera(fit, tops, lo, hi, objects_only, groups)


def explained_only(fit: FitResult) -> FitResult:
    """벽 경계 해가 설명하지 못하는 관측(경계점 대부분이 모델 선에서 UNEXPLAINED_PX 넘게 먼 것)을 뺀 해.
    해는 관측된 경계를 가장 길게 설명하는 것으로 골랐으므로, 그 해와 동떨어진 관측은 다른 경계(물체 윗면, 그림자,
    잘못 짚은 모서리)를 잡은 것이다. 남겨 두면 뒤에서 다시 맞출 때 그 관측이 화각을 끌고 간다."""
    keep = {}
    for k, s in fit.obs.items():
        ln = obs_line(fit.model, fit.x, k, s)
        if ln is not None and float((np.abs(s.points @ ln[0] - ln[1]) <= UNEXPLAINED_PX).mean()) >= MIN_EXPLAINED:
            keep[k] = s
    if len(keep) == len(fit.obs) or not any(not k.startswith("vertical") for k in keep):
        return fit
    return FitResult(fit.x, fit.model, keep, fit.rms, fit.supported)


def object_fit(analyses: list[dict], W: int, H: int, E: EdgeImage, prior: Prior) -> FitResult:
    """벽 경계를 하나도 잡지 못한 사진(가까운 물체가 벽과 바닥을 가린 사진)의 시작 해: 방 없이 표본 중앙값 카메라와
    물체 옆선(연직선)만 둔다. 화각·피치·롤은 뒤에서 원기둥 윗면 타원과 표본 지평선을 더해 푼다."""
    model = RoomModel(W, H, frozenset(), False)
    x0 = prior.x.copy()
    obs = {f"vertical_{i}": sg for i, sg in
           enumerate(object_verticals(E, model, x0, consensus_boxes(analyses), VERT_RADIUS))}
    return FitResult(x0, model, obs)


def _refit_with_terms(fit: FitResult, prior: Prior, tops: list, v0: float | None, W: int,
                      H: int, groups: list) -> tuple[FitResult, Prior]:
    """타원·상자 선분·지평선 항이 있으면 사전값에 더해 다시 맞춘 (해, 사전값). 없으면 그대로."""
    extra = camera_terms(tops, v0, W, H, groups)
    if extra is None:
        return fit, prior
    prior = Prior(prior.x, prior.sig, extra)
    return FitResult(refit_with_tops(fit, prior), fit.model, fit.obs, fit.rms, fit.supported), prior


def _log_profile(log, profile: list, lo: float, hi: float, fit: FitResult, tops: list, v0: float | None,
                 groups: list) -> None:
    cmin = min(p[1] for p in profile)
    log("  화각 프로파일 " + " ".join(f"{f:.0f}:{c - cmin:.1f}" for f, c, _ in profile)
        + f" → 범위 {lo:.1f}~{hi:.1f}, 선 {sorted(fit.obs)}, 타원 {len(tops)}개"
        + f", 상자 선분 {[len(g.segments) for g in groups]}" + (f", 지평선 {v0:.0f}" if v0 is not None else ""))


def _camera(fit: FitResult, tops: list, lo: float, hi: float, objects_only: bool, groups: list) -> dict:
    """맞춘 해 → 카메라 dict. basis는 근거: objects(물체만), image(사진이 화각을 정함), image-weak(정하지 못함)."""
    x = fit.x
    basis = "objects" if objects_only else ("image" if hi - lo <= DETERMINED_DEG else "image-weak")
    return {"vfov": float(x[0]), "pitch": float(x[1]), "roll": float(x[2]), "basis": basis,
            "fov_range": [round(lo, 1), round(hi, 1)], "edges": sorted(fit.obs), "ellipses": len(tops),
            "box_segments": sum(len(g.segments) for g in groups), "supported_px": round(fit.supported)}
