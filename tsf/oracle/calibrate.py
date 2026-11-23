"""채점기 검증: 기준 앱(정답·빈 장면·오차 주입)으로 채점기가 좋은 결과와 나쁜 결과를 구분하는지 본다.

같은 검증이 factory-eval 이슈의 수용 조건이다. 평가 자산을 바꾼 뒤 이 검증을 통과해야 수용된다.
"""

from __future__ import annotations

import json

from ..config import ROOT
from .apps import calibration_app
from .lab import Lab
from .policy import PUBLIC_SEEDS
from .runner import Fixture, make_fixtures, new_eval_dir, print_summary, run_eval
from .scenes import generate_scene

NOISE_LEVELS = (0.0, 0.1, 0.2, 0.4)


def _determinism(lab: Lab) -> tuple[str, bool, str]:
    a, b = Fixture("x", PUBLIC_SEEDS[0], "public"), Fixture("y", PUBLIC_SEEDS[0], "public")
    a.spec = b.spec = generate_scene(PUBLIC_SEEDS[0], "public")
    lab.render(a)
    lab.render(b)
    return "결정성", a.png == b.png, "같은 시드 두 번 렌더링 → 같은 PNG"


def calibrate() -> bool:
    out = new_eval_dir("calibrate")
    fixtures = make_fixtures("all")
    registry: dict = {}
    scores, checks = {}, []
    lab = Lab()
    try:
        checks.append(_determinism(lab))
        runs = [("gt", 0.0), ("empty", 0.0)] + [("noise", lv) for lv in NOISE_LEVELS]
        for kind, level in runs:
            label = kind if kind != "noise" else f"noise-{level:.1f}"
            app = calibration_app(kind, level, registry, out / label / "data")
            try:
                summary = run_eval(app, fixtures, out / label, lab, registry)
            finally:
                app.stop()
            print_summary(label, summary)
            scores[label] = summary["overall"]["fidelity"]
            checks.append((f"계약 스모크 ({label})", summary["smoke"]["passed"], "기준 앱이 계약대로 동작"))
    finally:
        lab.close()

    noise = [scores[f"noise-{lv:.1f}"] for lv in NOISE_LEVELS]
    checks += [
        ("정답 앱", scores["gt"] >= 95, f"{scores['gt']:.1f}점 (기준 95 이상)"),
        ("빈 앱", scores["empty"] <= 5, f"{scores['empty']:.1f}점 (기준 5 이하)"),
        ("오차 주입 앱", noise[0] >= 95 and all(x > y for x, y in zip(noise, noise[1:])),
         " > ".join(f"{x:.1f}" for x in noise) + " (오차 0%·10%·20%·40%, 단조 감소)"),
    ]
    print("\n채점기 검증")
    for name, ok, detail in checks:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name:<20} {detail}")
    (out / "calibration.json").write_text(json.dumps(
        {"scores": scores, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]},
        indent=2, ensure_ascii=False))
    print(f"\n결과: {out.relative_to(ROOT)}/calibration.json")
    return all(ok for _, ok, _ in checks)
