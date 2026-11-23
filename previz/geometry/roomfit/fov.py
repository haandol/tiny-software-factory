"""화각 다듬기와 확인: 타원·지평선 항을 더해 다시 맞추고, 화각 프로파일로 사진이 화각을 정했는지 잰다."""

from __future__ import annotations

import numpy as np

from .fit import FIT_ERRORS, FitResult, Prior, fit_points, total_cost, with_fov

FOV_STARTS = (24.0, 32.0, 40.0, 48.0, 56.0, 64.0, 72.0, 82.0, 95.0)   # 타원을 더해 다시 맞출 때의 화각 시작값 (도)
FOV_HOLD_SIG = 0.02           # 화각을 고정하고 나머지를 맞출 때의 화각 사전값 폭 (도)
PROFILE_STEPS = (-24.0, -16.0, -10.0, -6.0, -3.0, 3.0, 6.0, 10.0, 16.0, 24.0)   # 화각 프로파일을 잴 간격 (도)
PROFILE_SLACK = 6.0           # 견고 비용이 최소보다 이만큼 안 커지는 화각은 사진이 가려내지 못한다고 본다


def _cost_with_fov_prior(fit: FitResult, x: np.ndarray, prior: Prior) -> float:
    """전체 비용: 경계 + 타원(·지평선) + 화각 사전값."""
    return total_cost(fit.model, x, fit.obs, prior) + ((x[0] - prior.x[0]) / prior.sig[0]) ** 2


def refit_with_tops(fit: FitResult, prior: Prior) -> np.ndarray:
    """타원을 더해 다시 맞춘다. 벽 경계만으로 화각이 정해지지 않을 수 있으므로 화각 격자의 시작값마다 화각을 잠시
    묶어 나머지를 맞춘 뒤 풀어서 맞추고, 전체 비용(경계 + 타원 + 화각 사전값)이 가장 작은 해를 고른다."""
    best, best_c = fit.x, _cost_with_fov_prior(fit, fit.x, prior)
    for fov in (fit.fov,) + FOV_STARTS:
        hold = prior.held(fov, FOV_HOLD_SIG)
        try:
            x = fit_points(fit.model, with_fov(fit.x, fov), fit.obs, hold, rounds=3)
            x = fit_points(fit.model, x, fit.obs, prior, rounds=2, warm=True)
        except FIT_ERRORS:
            continue
        c = _cost_with_fov_prior(fit, x, prior)
        if np.all(np.isfinite(x)) and c < best_c - 1e-6:
            best, best_c = x, c
    return best


def fov_profile(fit: FitResult, prior: Prior) -> list[tuple[float, float, np.ndarray]]:
    """맞춘 해 둘레에서 화각을 고정하고 나머지를 다시 맞춘 (화각, 견고 비용, 해) 목록 (화각 순)."""
    out = [(fit.fov, total_cost(fit.model, fit.x, fit.obs, prior), fit.x)]
    for step in PROFILE_STEPS:
        fov = fit.fov + step
        if not 15.0 <= fov <= 120.0:
            continue
        hold = prior.held(fov, FOV_HOLD_SIG)
        try:
            x = fit_points(fit.model, with_fov(fit.x, fov), fit.obs, hold, rounds=3)
        except FIT_ERRORS:
            continue
        out.append((fov, total_cost(fit.model, x, fit.obs, prior), x))
    return sorted(out, key=lambda t: t[0])


def fov_interval(profile: list) -> tuple[float, float]:
    """비용이 최소 + PROFILE_SLACK 안에 드는, 최소 화각을 포함하는 이어진 화각 범위."""
    i0 = min(range(len(profile)), key=lambda i: profile[i][1])
    cmin = profile[i0][1]
    lo = hi = i0
    while lo > 0 and profile[lo - 1][1] <= cmin + PROFILE_SLACK:
        lo -= 1
    while hi < len(profile) - 1 and profile[hi + 1][1] <= cmin + PROFILE_SLACK:
        hi += 1
    return profile[lo][0], profile[hi][0]
