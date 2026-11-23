"""작은 비선형 최소제곱 풀이: Levenberg–Marquardt(수치 야코비안)와 여러 초기값 중 최선 고르기."""

from __future__ import annotations

from typing import Callable, Iterable

import numpy as np


def levenberg_marquardt(fun, x0: np.ndarray, iters: int = 80):
    """작은 Levenberg–Marquardt (수치 야코비안)."""
    x = x0.astype(float).copy()
    r = fun(x)
    cost = float(r @ r)
    lam = 1e-2
    for _ in range(iters):
        J = np.empty((r.size, x.size))
        for j in range(x.size):
            e = 1e-5 * max(1.0, abs(x[j]))
            xp = x.copy()
            xp[j] += e
            J[:, j] = (fun(xp) - r) / e
        A, g = J.T @ J, J.T @ r
        improved = False
        for _ in range(12):
            try:
                dx = np.linalg.solve(A + lam * (np.diag(np.diag(A)) + 1e-9 * np.eye(x.size)), -g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            r2 = fun(x + dx)
            c2 = float(r2 @ r2)
            if c2 < cost:
                x, r, cost = x + dx, r2, c2
                lam = max(lam / 3, 1e-9)
                improved = True
                break
            lam *= 5
        if not improved or float(np.abs(dx).max()) < 1e-7:
            break
    return x, cost


def best_of(fun, starts: Iterable[np.ndarray], iters: int = 80,
            accept: Callable[[np.ndarray], bool] | None = None) -> tuple[np.ndarray | None, float]:
    """초기값마다 LM을 돌려 비용이 가장 낮은 해 (해, 비용). 수치 오류로 실패한 시작점은 건너뛴다.

    accept가 있으면 그 조건을 만족하는 해만 고른다. 하나도 없으면 (None, inf).
    """
    best, best_cost = None, float("inf")
    for x0 in starts:
        try:
            x, cost = levenberg_marquardt(fun, x0, iters=iters)
        except (ValueError, FloatingPointError, OverflowError):
            continue
        if cost < best_cost and (accept is None or accept(x)):
            best, best_cost = x, cost
    return best, best_cost
