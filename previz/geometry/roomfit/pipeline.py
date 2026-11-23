"""사진으로 카메라 맞추기의 전체 흐름: 벽 경계(없으면 물체 옆선) → 타원·지평선 더해 다시 맞추기 → 화각 프로파일."""

from __future__ import annotations

import numpy as np

from ..edges import EdgeImage
from .boxes import consensus_boxes
from .fit import FitResult, Prior
from .fov import fov_interval, fov_profile, refit_with_tops
from .model import RoomModel
from .observe import VERT_RADIUS, object_verticals
from .samples import codex_horizon, sample_prior
from .terms import camera_terms, cylinder_tops
from .wallfit import calibrate

DETERMINED_DEG = 4.0          # 프로파일이 가려내지 못하는 화각 범위가 이보다 좁으면 사진이 화각을 정한 것이다


def image_camera(analyses: list[dict], W: int, H: int, img: np.ndarray, log=None) -> dict | None:
    """사진 경계(벽 모서리, 물체 옆선)와 원기둥 윗면 타원으로 맞춘 카메라 {"vfov", "pitch", "roll", "basis", ...}.
    벽 경계를 하나도 잡지 못하면 물체 옆선·타원·표본 지평선으로 맞추고, 타원도 없으면 None."""
    E = EdgeImage(img)
    prior = sample_prior(analyses, W, H)
    fit = calibrate(analyses, W, H, img, E)
    objects_only = fit is None or not np.all(np.isfinite(fit.x))
    if objects_only:
        fit = object_fit(analyses, W, H, E, prior)
    tops = cylinder_tops(E, analyses, fit.x)
    v0 = codex_horizon(analyses) if objects_only else None      # 벽 경계가 있으면 지평선은 경계가 정한다
    if objects_only and not tops:
        return None                                   # 옆선만으로는 화각이 정해지지 않는다
    fit, prior = _refit_with_terms(fit, prior, tops, v0, W, H)
    profile = fov_profile(fit, prior)
    lo, hi = fov_interval(profile)
    if log:
        _log_profile(log, profile, lo, hi, fit, tops, v0)
    return _camera(fit, tops, lo, hi, objects_only)


def object_fit(analyses: list[dict], W: int, H: int, E: EdgeImage, prior: Prior) -> FitResult:
    """벽 경계를 하나도 잡지 못한 사진(가까운 물체가 벽과 바닥을 가린 사진)의 시작 해: 방 없이 표본 중앙값 카메라와
    물체 옆선(연직선)만 둔다. 화각·피치·롤은 뒤에서 원기둥 윗면 타원과 표본 지평선을 더해 푼다."""
    model = RoomModel(W, H, frozenset(), False)
    x0 = prior.x.copy()
    obs = {f"vertical_{i}": sg for i, sg in
           enumerate(object_verticals(E, model, x0, consensus_boxes(analyses), VERT_RADIUS))}
    return FitResult(x0, model, obs)


def _refit_with_terms(fit: FitResult, prior: Prior, tops: list, v0: float | None, W: int,
                      H: int) -> tuple[FitResult, Prior]:
    """타원·지평선 항이 있으면 사전값에 더해 다시 맞춘 (해, 사전값). 없으면 그대로."""
    extra = camera_terms(tops, v0, W, H)
    if extra is None:
        return fit, prior
    prior = Prior(prior.x, prior.sig, extra)
    return FitResult(refit_with_tops(fit, prior), fit.model, fit.obs, fit.rms, fit.supported), prior


def _log_profile(log, profile: list, lo: float, hi: float, fit: FitResult, tops: list, v0: float | None) -> None:
    cmin = min(p[1] for p in profile)
    log("  화각 프로파일 " + " ".join(f"{f:.0f}:{c - cmin:.1f}" for f, c, _ in profile)
        + f" → 범위 {lo:.1f}~{hi:.1f}, 선 {sorted(fit.obs)}, 타원 {len(tops)}개"
        + (f", 지평선 {v0:.0f}" if v0 is not None else ""))


def _camera(fit: FitResult, tops: list, lo: float, hi: float, objects_only: bool) -> dict:
    """맞춘 해 → 카메라 dict. basis는 근거: objects(물체만), image(사진이 화각을 정함), image-weak(정하지 못함)."""
    x = fit.x
    basis = "objects" if objects_only else ("image" if hi - lo <= DETERMINED_DEG else "image-weak")
    return {"vfov": float(x[0]), "pitch": float(x[1]), "roll": float(x[2]), "basis": basis,
            "fov_range": [round(lo, 1), round(hi, 1)], "edges": sorted(fit.obs), "ellipses": len(tops),
            "supported_px": round(fit.supported)}
