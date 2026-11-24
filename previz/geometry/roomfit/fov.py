"""화각 다듬기와 확인: 타원·지평선 항을 더해 다시 맞추고, 화각 프로파일로 사진이 화각을 정했는지 잰다.
사진이 화각을 정하지 못하면 고정 사전값과 사진 비용으로 화각의 사후 평균을 낸다 (fov_posterior)."""

from __future__ import annotations

import numpy as np

from .fit import FIT_ERRORS, FitResult, Prior, fit_points, total_cost, with_fov

FOV_STARTS = (24.0, 32.0, 40.0, 48.0, 56.0, 64.0, 72.0, 82.0, 95.0)   # 타원을 더해 다시 맞출 때의 화각 시작값 (도)
FOV_HOLD_SIG = 0.02           # 화각을 고정하고 나머지를 맞출 때의 화각 사전값 폭 (도)
PROFILE_STEPS = (-24.0, -16.0, -10.0, -6.0, -3.0, 3.0, 6.0, 10.0, 16.0, 24.0)   # 화각 프로파일을 잴 간격 (도)
PROFILE_SLACK = 6.0           # 견고 비용이 최소보다 이만큼 안 커지는 화각은 사진이 가려내지 못한다고 본다
POSTERIOR_FOVS = np.arange(16.0, 111.0, 2.0)   # 사후 평균을 낼 때 화각을 고정해 맞추는 격자 (도)
POSTERIOR_STEP = 0.05         # 격자 사이 비용을 선형 보간해 사후 분포를 재는 간격 (도)
FOV_PRIOR_CENTER = 50.0       # 사진이 화각을 정하지 못할 때 기대는 고정 사전값: 중심 (도)
FOV_PRIOR_SIG = 10.0          # 폭 (도). 요청마다 흔들리는 Codex 화각 대신 쓴다


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


def chained_profile(fit: FitResult, prior: Prior) -> list[tuple[float, float, np.ndarray]]:
    """화각 격자 POSTERIOR_FOVS의 화각마다 고정하고 나머지를 맞춘 (화각, 견고 비용, 해) 목록 (화각 순).
    맞춘 해에 가장 가까운 격자 화각에서 시작해 양쪽으로 이웃 격자의 해를 시작값으로 이어 간다 (같은 해의 갈래를 따른다)."""
    grid = POSTERIOR_FOVS
    i0 = int(np.argmin(np.abs(grid - fit.fov)))
    out: dict[int, tuple[float, np.ndarray]] = {}
    for idx in (range(i0, len(grid)), range(i0, -1, -1)):
        x = fit.x
        for i in idx:
            fov = float(grid[i])
            try:
                x = fit_points(fit.model, with_fov(x, fov), fit.obs, prior.held(fov, FOV_HOLD_SIG), rounds=2, warm=True)
            except FIT_ERRORS:
                x = fit.x
                continue
            c = total_cost(fit.model, x, fit.obs, prior)
            if np.all(np.isfinite(x)) and np.isfinite(c) and (i not in out or c < out[i][0]):
                out[i] = (c, x)
    return [(float(grid[i]), out[i][0], out[i][1]) for i in sorted(out)]


def fov_posterior(fit: FitResult, prior: Prior) -> tuple[np.ndarray, float]:
    """사진이 화각을 정하지 못할 때의 해: 화각은 사후 평균, 나머지 변수는 그 화각에 고정해 다시 맞춘 값. (해, 사후 폭).

    화각 프로파일이 넓게 평평한 사진(벽 경계가 거의 없거나 정면을 보는 방)에서는 비용의 최솟값이 어디냐가 관측의
    작은 차이에 크게 흔들리고, 예전에는 그 사이를 요청마다 다른 Codex 화각(사전값 중심)이 정해서 같은 장면도 요청마다
    화각이 6° 넘게 달랐다. 여기서는 고정 사전값 N(FOV_PRIOR_CENTER, FOV_PRIOR_SIG²)과 사진 비용(exp(−비용/2))의 곱을
    사후 분포로 보고 그 평균을 쓴다. 평균은 비용의 작은 차이에 조금만 움직이고, Codex 값에는 기대지 않는다."""
    prof = chained_profile(fit, prior)
    if len(prof) < 3:
        return fit.x, float("nan")
    f = np.array([p[0] for p in prof])
    c = np.array([p[1] for p in prof])
    fine = np.arange(f[0], f[-1] + 1e-9, POSTERIOR_STEP)
    ci = np.interp(fine, f, c)
    logw = -(ci - ci.min()) / 2.0 - 0.5 * ((fine - FOV_PRIOR_CENTER) / FOV_PRIOR_SIG) ** 2
    w = np.exp(logw - logw.max())
    mean = float((w * fine).sum() / w.sum())
    sd = float(np.sqrt((w * (fine - mean) ** 2).sum() / w.sum()))
    start = prof[int(np.argmin(np.abs(f - mean)))][2]
    try:
        x = fit_points(fit.model, with_fov(start, mean), fit.obs, prior.held(mean, FOV_HOLD_SIG), rounds=2, warm=True)
    except FIT_ERRORS:
        x = with_fov(start, mean)
    return (x if np.all(np.isfinite(x)) else with_fov(start, mean)), sd
