"""관측 경계에 방 모델 맞추기: 잔차와 견고 무게, LM 되풀이, 선 빼 보기, 설명한 경계 길이·견고 비용.

경계점의 위치 오차는 경계의 대비(신호 대 잡음비)에 반비례한다고 본다 (line_sigma). 질감·노이즈 속에서 잘못 잡은
흐린 선 하나가 뚜렷한 선·물체 옆선·타원을 모두 이겨 화각을 10° 넘게 끌던 일을 막는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..edges import Segment, clip_segment
from ..lm import levenberg_marquardt
from .model import RoomModel

SIG_PX = 1.0                  # 뚜렷한 경계점 하나의 위치 오차 (픽셀)
CONTRAST_REF = 8.0            # 둘레보다 이만큼 뚜렷하지 않은 경계는 위치 오차를 (이 값 ÷ 대비)배로 본다
HUBER_PX = 1.5                # 이보다 먼 경계점은 무게를 줄인다
CAUCHY_PX = 2.0               # 다시 맞출 때 Cauchy 무게의 폭 (픽셀)
FIT_ERRORS = (ValueError, FloatingPointError, np.linalg.LinAlgError)    # 맞추기 하나가 실패하면 그 시작값만 버린다


@dataclass
class Prior:
    x: np.ndarray                  # 사전값 중심
    sig: np.ndarray                # 사전값 폭
    extra: object = None           # 더할 잔차 함수 x → 배열 (원기둥 윗면 타원 등). 없으면 None

    def held(self, fov: float, sig_fov: float) -> Prior:
        """화각만 fov에 폭 sig_fov로 묶은 사전값 (나머지 변수와 extra는 그대로)."""
        return Prior(np.r_[fov, self.x[1:]], np.r_[sig_fov, self.sig[1:]], self.extra)


@dataclass
class FitResult:
    x: np.ndarray
    model: RoomModel
    obs: dict = field(default_factory=dict)        # 모서리 이름 → Segment
    rms: float = float("inf")
    supported: float = 0.0                          # 모델 선에서 1.5픽셀 안에 잡힌 경계 길이 (픽셀)

    @property
    def fov(self) -> float:
        return float(self.x[0])


def with_fov(x: np.ndarray, fov: float) -> np.ndarray:
    """화각만 fov로 바꾼 x의 사본."""
    x = x.copy()
    x[0] = fov
    return x


def _clamp_fov(x: np.ndarray) -> np.ndarray:
    x[0] = min(max(x[0], 15.0), 120.0)
    return x


def line_sigma(s: Segment) -> float:
    """경계점 하나의 위치 오차 (픽셀). 경계를 잡는 오차는 신호 대 잡음비에 반비례하므로, 둘레(질감·노이즈)보다
    조금만 뚜렷한 경계는 그만큼 덜 믿는다. 질감 속에서 잘못 잡은 흐린 선 하나가 뚜렷한 선 여럿을 이기지 못하게 한다."""
    return SIG_PX * max(1.0, CONTRAST_REF / max(s.contrast, 1e-6))


def obs_line(model: RoomModel, x, k: str, s: Segment):
    """관측 k에 대응하는 모델의 영상 직선 (n, c): 물체 옆선이면 그 점들을 지나는 연직선, 아니면 방 모서리."""
    return model.vertical_line(x, s.points.mean(0)) if k.startswith("vertical") else model.line(x, k)


def _residual_fn(model: RoomModel, obs: dict, weights: dict, prior: Prior):
    names = [k for k in obs if k in model.edges() or k.startswith("vertical")]

    def fun(x):
        r = [(x - prior.x) / prior.sig]
        for k in names:
            pts = obs[k].points
            ln = obs_line(model, x, k, obs[k])
            if ln is None:
                r.append(np.full(len(pts), 50.0))
                continue
            n, c = ln
            r.append(weights[k] * (pts @ n - c) / line_sigma(obs[k]))
        if prior.extra is not None:
            r.append(prior.extra(x))
        return np.concatenate(r)
    return fun


def _weights(model: RoomModel, x, obs: dict, cauchy: bool) -> dict:
    out = {}
    for k, sg in obs.items():
        ln = obs_line(model, x, k, sg)
        if ln is None:
            out[k] = np.ones(len(sg.points))
            continue
        r = np.abs(sg.points @ ln[0] - ln[1]) * SIG_PX / line_sigma(sg)
        w = 1.0 / (1.0 + (r / CAUCHY_PX) ** 2) if cauchy else np.where(r <= HUBER_PX, 1.0, HUBER_PX / np.maximum(r, 1e-9))
        out[k] = np.sqrt(w)
    return out


def fit_points(model: RoomModel, x0: np.ndarray, obs: dict, prior: Prior, rounds: int = 3,
               warm: bool = False) -> np.ndarray:
    """경계점과 투영 모서리의 거리를 최소제곱으로 줄인다. 처음은 Huber, 그다음부터는 Cauchy 무게로 되풀이해
    다른 경계를 잡은 선(수십 픽셀 떨어진 점들)이 답을 끌지 못하게 한다. warm이면 x0가 이미 좋은 해라고 보고
    처음부터 x0에서 잰 Cauchy 무게를 쓴다."""
    x = x0.copy()
    weights = _weights(model, x, obs, cauchy=True) if warm else {k: np.ones(len(v.points)) for k, v in obs.items()}
    for i in range(rounds):
        x, _ = levenberg_marquardt(_residual_fn(model, obs, weights, prior), x, iters=60)
        _clamp_fov(x)
        weights = _weights(model, x, obs, cauchy=warm or i > 0)
    return x


def fit_to_codex(model: RoomModel, x0: np.ndarray, lines: dict, prior: Prior) -> np.ndarray:
    """Codex가 잰 벽 선 위의 점들로 모델을 먼저 맞춘다 (경계 찾기의 초기값)."""
    obs = {}
    for k, (p0, p1) in lines.items():
        if k not in model.edges():
            continue
        c = clip_segment(p0, p1, model.W, model.H, margin=0.0)
        if c is None:
            continue
        t = np.linspace(0, 1, 9)[:, None]
        pts = c[0][None] + t * (c[1] - c[0])[None]
        n = np.array([0.0, 1.0])
        obs[k] = Segment(pts, n, 0.0, 1.0, CONTRAST_REF)
    if not obs:
        return x0
    sig_scale = Prior(prior.x, prior.sig)
    weights = {k: np.full(len(s.points), SIG_PX / 4.0) for k, s in obs.items()}   # Codex 점은 4픽셀 정도 흔들린다
    x, _ = levenberg_marquardt(_residual_fn(model, obs, weights, sig_scale), x0, iters=80)
    return _clamp_fov(x)


def fit_leave_one_out(model: RoomModel, x: np.ndarray, obs: dict, prior: Prior) -> list[tuple[np.ndarray, str]]:
    """벽 선을 하나씩 빼고 다시 맞춘 해와 뺀 선 (다른 경계를 잡은 선이 하나 있으면 그 선을 뺀 해가 나머지를 잘 맞춘다)."""
    walls = [k for k in obs if not k.startswith("vertical")]
    if len(walls) <= 2:
        return []
    out = []
    for k in walls:
        sub = {j: v for j, v in obs.items() if j != k}
        try:
            out.append((fit_points(model, x, sub, prior, rounds=2, warm=True), k))
        except FIT_ERRORS:
            continue
    return out


def prune_lines(model: RoomModel, x: np.ndarray, obs: dict, prior: Prior, max_drop: int = 2) -> list[np.ndarray]:
    """선을 하나씩 빼 보며, 관측 전체를 더 길게 설명하게 되면 그 선을 빼고 다음 선을 또 빼 본다 (최대 max_drop개).
    틀린 선이 둘(예: 다른 경계를 잡은 바닥선과 그 교점에서 찾은 모서리)이어도 걸러낸다. 거쳐 간 해들을 돌려준다."""
    out = []
    cur, cur_obs = x, dict(obs)
    cur_sup = evaluate(model, cur, obs)[1]
    for _ in range(max_drop):
        tries = fit_leave_one_out(model, cur, cur_obs, prior)
        if not tries:
            break
        scored = [(evaluate(model, x2, obs)[1], x2, k) for x2, k in tries]
        out += [x2 for _, x2, _ in scored]
        sup, x2, k = max(scored, key=lambda t: t[0])
        if sup <= cur_sup + 20.0:
            break
        cur, cur_sup = x2, sup
        cur_obs = {j: v for j, v in cur_obs.items() if j != k}
    return out


def evaluate(model: RoomModel, x: np.ndarray, obs: dict) -> tuple[float, float]:
    """(경계점 잔차 RMS, 모델 선에서 1.5픽셀 안에 든 경계 길이)."""
    res, supported = [], 0.0
    for k, s in obs.items():
        ln = obs_line(model, x, k, s)
        if ln is None:
            continue
        r = s.points @ ln[0] - ln[1]
        res.append(r)
        supported += float((np.abs(r) <= 1.5).sum()) * 2.0 * (SIG_PX / line_sigma(s)) ** 2
    if not res:
        return float("inf"), 0.0
    r = np.concatenate(res)
    return float(np.sqrt(np.mean(np.minimum(r * r, 25.0)))), supported


def robust_cost(model: RoomModel, x: np.ndarray, obs: dict) -> float:
    """경계점과 투영 모서리 거리의 Cauchy 비용 합 (멀리 떨어진 점 하나가 비용을 키우지 못한다)."""
    c = 0.0
    for k, s in obs.items():
        ln = obs_line(model, x, k, s)
        if ln is None:
            c += CAUCHY_PX ** 2 * math.log1p((25.0 / CAUCHY_PX) ** 2) * len(s.points)
            continue
        r = (s.points @ ln[0] - ln[1]) / line_sigma(s)
        c += float(np.sum(CAUCHY_PX ** 2 * np.log1p((r / CAUCHY_PX) ** 2)))
    return c


def total_cost(model: RoomModel, x: np.ndarray, obs: dict, prior: Prior) -> float:
    """경계의 견고 비용 + 타원 잔차 제곱합."""
    c = robust_cost(model, x, obs)
    if prior.extra is not None:
        c += float(np.sum(prior.extra(x) ** 2))
    return c
