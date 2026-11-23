"""같은 사진을 여러 번 잰 Codex 측정값 가운데 장면으로 쓸 하나를 고른다.

Codex의 픽셀 측정(지평선, 벽 선, 화각)은 같은 사진이라도 잴 때마다 흔들리고, 카메라 보정이 그 흔들림을 키운다.
측정값마다 장면 명세를 그대로 풀고(previz/geometry, 바꾸지 않는다), 풀린 카메라에서 보정 잔차가 가장 작은 측정값,
곧 Codex의 화각·지평선 추정과 벽 선·상자 모서리 관측이 서로 가장 잘 맞는 측정값의 장면을 쓴다.
벽 선이 둘뿐이면 어떤 화각으로도 잔차가 거의 0이 되어 잔차로는 가릴 수 없으므로, 표본들이 푼 화각의 중앙값에서
멀수록 작은 벌점을 더한다 (잔차가 뚜렷이 다르면 잔차가 정하고, 비슷하면 다른 표본과 맞는 쪽을 고른다).
측정값을 섞지 않으므로 고른 장면은 그 측정값 하나로 푼 결과와 같다.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import numpy as np

from .config import log
from .geometry.calibration import camera_fit_cost
from .geometry.spec import build_spec

MIN_CONSTRAINTS = 2          # 선·상자 관측이 이만큼 있어야 보정을 하고, 잔차를 견줄 뜻이 있다
AGREEMENT_SCALE_DEG = 5.0    # 표본 화각 중앙값에서 이만큼 벗어나면
AGREEMENT_WEIGHT = 0.01      # 잔차(제곱 평균)에 이만큼 더한다 (벗어난 정도의 제곱에 비례)


@dataclass
class Candidate:
    sample: int              # 표본 번호 (1부터)
    analysis: dict
    spec: dict
    cost: float              # 보정 잔차 제곱 평균 (작을수록 측정값이 스스로 잘 맞는다)
    constraints: int         # 보정에 쓴 선·상자 관측 수

    @property
    def vfov(self) -> float:
        return self.spec["camera_info"]["vfov"]

    def score(self, median_vfov: float) -> float:
        """고를 때 견주는 값: 보정 잔차 + 다른 표본들과 화각이 다른 만큼의 벌점."""
        return self.cost + AGREEMENT_WEIGHT * ((self.vfov - median_vfov) / AGREEMENT_SCALE_DEG) ** 2


def evaluate(sample: int, analysis: dict, W: int, H: int, img: np.ndarray | None) -> Candidate:
    spec = build_spec(analysis, W, H, img)
    ci = spec["camera_info"]
    cost, constraints = camera_fit_cost(analysis, W, H, ci["vfov"], ci["pitch"], ci["roll"])
    return Candidate(sample, analysis, spec, cost, constraints)


def choose(candidates: list[Candidate]) -> Candidate:
    """물체를 찾은 측정값, 그중에서도 선 관측으로 보정한 측정값을 먼저 보고, score가 가장 작은 것을 고른다."""
    pool = [c for c in candidates if c.spec["objects"]] or candidates
    pool = [c for c in pool if c.constraints >= MIN_CONSTRAINTS] or pool
    median_vfov = statistics.median(c.vfov for c in pool)
    return min(pool, key=lambda c: (c.score(median_vfov), c.sample))


def pick(analyses: list[tuple[int, dict]], W: int, H: int, img: np.ndarray | None) -> tuple[Candidate, list[Candidate]]:
    """(고른 후보, 장면을 푼 모든 후보). 장면을 풀지 못한 측정값은 빼고, 하나도 못 풀면 예외를 낸다."""
    candidates, last_error = [], None
    for sample, analysis in analyses:
        try:
            candidates.append(evaluate(sample, analysis, W, H, img))
        except Exception as e:                    # noqa: BLE001 - 측정값 하나가 이상해도 나머지로 장면을 만든다
            last_error = e
            log(f"표본 #{sample}로 장면을 풀지 못했다: {e}")
    if not candidates:
        raise RuntimeError(f"측정값으로 장면을 풀지 못했다: {last_error}")
    return choose(candidates), candidates
