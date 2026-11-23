"""채점기 검증: 기준 앱(정답·빈 장면·오차 주입·흔들림)으로 채점기가 좋은 결과와 나쁜 결과를 구분하는지 본다.

같은 검증이 factory-eval 이슈의 수용 조건이다. 평가 자산을 바꾼 뒤 이 검증을 통과해야 수용된다.
"""

from __future__ import annotations

import json

from ..config import ROOT
from .apps import calibration_app
from .lab import Lab
from .policy import FINAL_TARGETS, PUBLIC_SEEDS, STABILITY_PIXEL_NOISE
from .runner import Fixture, make_fixtures, new_eval_dir, print_summary, repeat_seed, run_eval
from .scenes import generate_scene

NOISE_LEVELS = (0.0, 0.1, 0.2, 0.4)
JITTER_LEVEL = 0.2          # 흔들리는 앱: 요청마다 오차 수준을 0~20%에서 새로 뽑는다
STABILITY_KEYS = ("stability.fidelity_delta", "stability.fov_delta_deg")


def _determinism(lab: Lab) -> tuple[str, bool, str]:
    a, b = Fixture("x", PUBLIC_SEEDS[0], "public"), Fixture("y", PUBLIC_SEEDS[0], "public")
    a.spec = b.spec = generate_scene(PUBLIC_SEEDS[0], "public")
    lab.render(a)
    lab.render(b)
    return "결정성", a.png == b.png, "같은 시드 두 번 렌더링 → 같은 PNG"


def _repeat_inputs(lab: Lab, fixtures: list[Fixture]) -> tuple[str, bool, str]:
    """두 번째 요청 이미지: 바이트는 다르고, 픽셀은 채널마다 ±STABILITY_PIXEL_NOISE 이내로만 다르고, 결정적이다."""
    rows = [fx for fx in fixtures if fx.repeated]
    diffs = [lab.pixel_diff(fx.png, fx.repeat_png) for fx in rows]
    distinct = all(fx.repeat_png and fx.repeat_png != fx.png for fx in rows)
    small = all(0 < d["max"] <= STABILITY_PIXEL_NOISE for d in diffs)
    stable = all(lab.repeat_image(fx.png, repeat_seed(fx)) == fx.repeat_png for fx in rows)
    worst = max((d["max"] for d in diffs), default=0)
    return ("두 번째 요청 입력", bool(rows) and distinct and small and stable,
            f"홀드아웃 {len(rows)}장: 바이트 {'다름' if distinct else '같음'}, 채널 최대 차 {worst} "
            f"(기준 1~{STABILITY_PIXEL_NOISE}), 다시 만들면 {'같은' if stable else '다른'} 바이트")


def _stability_checks(stab: dict[str, dict]) -> list[tuple[str, bool, str]]:
    """결정적인 기준 앱은 두 지표가 0, 흔들리는 앱은 두 지표가 최종 목표를 넘어야 흔들림을 잡아낸 것이다."""
    def fmt(m):
        return ", ".join(f"{name} {'-' if m.get(k) is None else f'{m[k]:.2f}'}{unit}"
                         for k, name, unit in zip(STABILITY_KEYS, ("충실도 차", "화각 차"), ("", "°")))
    fixed = {label: m for label, m in stab.items() if label != "jitter"}
    zero = all(m.get(k) == 0 for m in fixed.values() for k in STABILITY_KEYS)
    bad = [f"{label} {fmt(m)}" for label, m in fixed.items() if any(m.get(k) != 0 for k in STABILITY_KEYS)]
    j = stab["jitter"]
    caught = all(j.get(k) is not None and j[k] > FINAL_TARGETS[k][1] for k in STABILITY_KEYS)
    limits = ", ".join(f"{k.split('.', 1)[1]} > {FINAL_TARGETS[k][1]:g}" for k in STABILITY_KEYS)
    return [
        ("안정성 (결정적 앱)", zero, f"{len(fixed)}개 앱 모두 0" if zero else "0이 아니다: " + "; ".join(bad)),
        ("안정성 (흔들리는 앱)", caught, f"{fmt(j)} (기준 최종 목표 초과: {limits})"),
    ]


def calibrate() -> bool:
    out = new_eval_dir("calibrate")
    fixtures = make_fixtures("all")
    registry: dict = {}
    scores, stab, checks = {}, {}, []
    lab = Lab()
    try:
        checks.append(_determinism(lab))
        runs = [("gt", 0.0), ("empty", 0.0)] + [("noise", lv) for lv in NOISE_LEVELS] + [("jitter", JITTER_LEVEL)]
        for kind, level in runs:
            label = {"noise": f"noise-{level:.1f}"}.get(kind, kind)
            app = calibration_app(kind, level, registry, out / label / "data")
            try:
                summary = run_eval(app, fixtures, out / label, lab, registry)
            finally:
                app.stop()
            print_summary(label, summary)
            scores[label] = summary["overall"]["fidelity"]
            stab[label] = {k: summary["holdout"]["metrics"].get(k) for k in STABILITY_KEYS}
            checks.append((f"계약 스모크 ({label})", summary["smoke"]["passed"], "기준 앱이 계약대로 동작"))
        checks.append(_repeat_inputs(lab, fixtures))
    finally:
        lab.close()

    noise = [scores[f"noise-{lv:.1f}"] for lv in NOISE_LEVELS]
    checks += [
        ("정답 앱", scores["gt"] >= 95, f"{scores['gt']:.1f}점 (기준 95 이상)"),
        ("빈 앱", scores["empty"] <= 5, f"{scores['empty']:.1f}점 (기준 5 이하)"),
        ("오차 주입 앱", noise[0] >= 95 and all(x > y for x, y in zip(noise, noise[1:])),
         " > ".join(f"{x:.1f}" for x in noise) + " (오차 0%·10%·20%·40%, 단조 감소)"),
    ] + _stability_checks(stab)
    print("\n채점기 검증")
    for name, ok, detail in checks:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name:<20} {detail}")
    (out / "calibration.json").write_text(json.dumps(
        {"scores": scores, "stability": stab,
         "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]},
        indent=2, ensure_ascii=False))
    print(f"\n결과: {out.relative_to(ROOT)}/calibration.json")
    return all(ok for _, ok, _ in checks)
