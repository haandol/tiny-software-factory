"""5단계: 발견 루프. 배포된 main을 평가해, 최종 목표에 못 미친 지표를 단계적 목표의 Linear 이슈로 만든다.

  1. 제품 main 커밋이나 평가 자산 커밋이 바뀔 때마다 한 번, 새 홀드아웃 시드로 main을 평가한다
  2. FINAL_TARGETS에 못 미친 지표를 찾는다 (지표 하나당 이슈 하나)
  3. 다음 목표는 현재값과 최종 목표의 중간이다. 최종 목표를 한 번에 요구하면 통과하지 못하고 사람에게 넘어가기 때문이다
     목표와의 차이가 그 지표의 측정 잡음(평가 정책의 METRIC_NOISE)보다 작으면 이슈를 만들지 않는다 (SWF-10의 교훈)
  4. 같은 지표의 이슈가 열려 있으면 만들지 않는다. 한 번에 최대 2개, 열린 자동 이슈는 최대 3개
  5. 모든 지표가 최종 목표에 닿으면 이슈를 만들지 않는다
이슈의 '관찰'은 발견 에이전트가 장면별 수치를 보고 쓴다. 판단과 이슈 생성 규칙은 프로그램이 정한다.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime

from . import linear
from .config import PRODUCT, ROOT, VAR
from .env import agent_env, git
from .oracle import FINAL_TARGETS, Lab, make_fixtures, meets, new_eval_dir, run_eval, start_product_app

STATE_FILE = VAR / "discovery.json"
MAX_NEW_PER_RUN = 2
MAX_OPEN_AUTO = 3
OPEN_STATES = ["Backlog", "Todo", "In Progress", linear.NEEDS_HUMAN]

LABELS = {
    "camera.fov_error_deg": "카메라 화각 오차", "camera.pitch_error_deg": "카메라 피치 오차",
    "camera.roll_error_deg": "카메라 롤 오차", "objects.f1": "물체 검출 F1", "objects.position_error": "물체 위치 오차",
    "objects.size_error": "물체 크기 오차", "objects.color_delta_e": "물체 색 오차 ΔE",
    "reprojection.iou": "실루엣 IoU", "stability.fidelity_delta": "같은 장면 재생성 시 충실도 차",
    "stability.fov_delta_deg": "같은 장면 재생성 시 화각 차",
}
SUITE_NAMES = {"public": "공개 세트", "holdout": "홀드아웃"}


def marker(suite: str, metric: str) -> str:
    return f"지표 키: `{suite}:{metric}`"


def metric_noise() -> dict[str, float]:
    """평가 정책에 기록된 지표별 측정 잡음 (SWF-11). 없으면 0으로 본다."""
    from .oracle import policy
    return dict(getattr(policy, "METRIC_NOISE", {}))


def staged_target(op: str, final: float, actual: float, noise: float = 0.0) -> float:
    """다음 목표: 현재값과 최종 목표의 중간. 한 단계의 폭은 측정 잡음보다 작지 않게 하고, 최종 목표를 넘지 않는다."""
    step = max(abs(final - actual) / 2, noise)
    nxt = actual - step if op == "<=" else actual + step
    nxt = max(nxt, final) if op == "<=" else min(nxt, final)
    if abs(nxt - final) < max(abs(final) * 0.1, 0.05):
        nxt = final
    return round(nxt, 2 if abs(final) < 1 else 1)


def gaps(summary: dict, noise: dict[str, float] | None = None) -> tuple[list[dict], list[dict]]:
    """(최종 목표 미달 지표, 잡음 안이라 건너뛴 지표). 두 세트에서 모두 못 미치면 홀드아웃으로 하나만 낸다.

    목표와의 차이가 측정 잡음보다 작으면 이슈를 만들지 않는다. 잡음보다 작은 차이는 개선이 아니라 운을 쫓게 된다.
    """
    noise = metric_noise() if noise is None else noise
    found, within = {}, {}
    for suite in ("public", "holdout"):
        metrics = summary.get(suite, {}).get("metrics", {})
        for key, (op, final) in FINAL_TARGETS.items():
            value = metrics.get(key)
            if value is None or meets(op, value, final):
                continue
            gap = {"suite": suite, "metric": key, "op": op, "final": final, "actual": value,
                   "noise": noise.get(key, 0.0)}
            if abs(value - final) <= gap["noise"]:
                within[key] = gap
                found.pop(key, None)
            else:
                found[key] = gap | {"next": staged_target(op, final, value, gap["noise"])}
                within.pop(key, None)
    order = list(FINAL_TARGETS)
    return (sorted(found.values(), key=lambda g: order.index(g["metric"])),
            sorted(within.values(), key=lambda g: order.index(g["metric"])))


def worst_fixtures(summary: dict, gap: dict, n: int = 3) -> list[dict]:
    rows = [f for f in summary["fixtures"] if f["variant"] == gap["suite"] and gap["metric"] in f["metrics"]]
    rows.sort(key=lambda f: f["metrics"][gap["metric"]], reverse=(gap["op"] == "<="))
    return rows[:n]


OBSERVE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["observation", "hypothesis"],
                  "properties": {"observation": {"type": "string"}, "hypothesis": {"type": "string"}}}


def observe(summary: dict, gap: dict) -> dict:
    """발견 에이전트: 장면별 수치를 보고 관찰 한 문장과 원인 가설 한 문장을 쓴다. 실패하면 빈 값."""
    rows = "\n".join(f"- {f['fixture']} (시드 {f['seed']}): {gap['metric']}={f['metrics'][gap['metric']]}, "
                     f"충실도 {f['fidelity']}, 화각 오차 {f['metrics'].get('camera.fov_error_deg')}, "
                     f"F1 {f['metrics'].get('objects.f1')}" for f in summary["fixtures"] if f["variant"] == gap["suite"])
    prompt = (f"프리비즈 앱의 평가 결과다. 지표 {gap['metric']}의 {SUITE_NAMES[gap['suite']]} 평균이 {gap['actual']}로 "
              f"최종 목표 {gap['op']} {gap['final']}에 못 미친다.\n\n장면별 수치\n{rows}\n\n"
              "observation: 수치에서 보이는 사실 한 문장 (어떤 장면에서 어떻게 나쁜가).\n"
              "hypothesis: 원인 가설 한 문장. 확인되지 않은 것은 가설로만 쓴다. 해결 방법은 쓰지 않는다.")
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(["claude", "-p", prompt, "--bare", "--output-format", "json", "--json-schema",
                              json.dumps(OBSERVE_SCHEMA), "--max-budget-usd", "0.3"],
                             cwd=tmp, env=agent_env(), capture_output=True, text=True, timeout=300)
    try:
        result = json.loads(out.stdout)
        return result["structured_output"] | {"cost_usd": result.get("total_cost_usd")}
    except (ValueError, KeyError, TypeError):
        return {"observation": "", "hypothesis": "", "cost_usd": None}


def issue_body(gap: dict, summary: dict, commit: str, eval_dir, seen: dict) -> str:
    label, suite = LABELS.get(gap["metric"], gap["metric"]), SUITE_NAMES[gap["suite"]]
    worst = "\n".join(f"| {f['fixture']} | {f['seed']} | {f['metrics'][gap['metric']]} | {f['fidelity']} |"
                      for f in worst_fixtures(summary, gap))
    obs = f"\n## 관찰 (발견 에이전트)\n- {seen['observation']}\n- 가설: {seen['hypothesis']}\n" if seen.get("observation") else ""
    return f"""(발견 루프가 만들었다. 배포된 main `{commit}`을 새 홀드아웃 시드로 평가한 결과에서 최종 목표에 못 미친 지표다.)

## 근거
{suite} `{gap['metric']}` 평균 {gap['actual']}, 최종 목표 {gap['op']} {gap['final']}. 평가 기록: `{eval_dir}`

| 장면 | 시드 | {gap['metric']} | 충실도 |
|---|---|---|---|
{worst}
{obs}
## 원하는 결과
- {suite} {label}(`{gap['metric']}`) 평균 {gap['next']:g} {"이하" if gap['op'] == "<=" else "이상"}
- 이번 목표는 현재값과 최종 목표({gap['final']:g}) 사이의 중간 단계다. 최종 목표는 다음 이슈에서 이어서 다룬다.
- 이 지표의 측정 잡음은 {gap['noise']:g}다 (평가 정책 METRIC_NOISE). 목표와의 차이가 이보다 커서 이슈를 만들었다.

## 제약
- Codex는 지금처럼 codex app-server와 CODEX_HOME 설정의 모델로 부른다.
- 다른 지표가 나빠지지 않는다 (홀드아웃 회귀 허용폭 안).

{marker(gap['suite'], gap['metric'])}
"""


def evaluate_main(lab: Lab, commit: str) -> tuple[dict, object]:
    out = new_eval_dir(f"discovery-{commit}")
    app = start_product_app(PRODUCT, out / "data", out / "app.log")
    try:
        return run_eval(app, make_fixtures("all"), out, lab), out
    finally:
        app.stop()


def discover(lab: Lab, force: bool = False, log=print) -> list[dict]:
    """main을 평가해 목표 미달 지표를 이슈로 만든다. 만든 이슈 목록을 돌려준다."""
    if not (PRODUCT / "app.py").exists():
        return []
    commit = git("rev-parse", "--short", "HEAD", cwd=PRODUCT)
    # 평가 기준이 바뀌어도(factory-eval 병합) 제품 커밋은 그대로이므로, 평가 자산의 커밋도 함께 본다
    oracle = git("log", "-1", "--format=%h", "--", "tsf/oracle", "CONTRACT.md", cwd=ROOT)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    if state.get("commit") == commit and state.get("oracle") == oracle and not force:
        return []
    open_auto = [i for i in linear.fetch_issues(OPEN_STATES) if "auto-discovered" in i["labels"]]
    if len(open_auto) >= MAX_OPEN_AUTO:
        log(f"발견 루프: 열린 자동 이슈가 {len(open_auto)}개라 새로 만들지 않는다")
        return []
    log(f"발견 루프: main {commit} 평가")
    summary, eval_dir = evaluate_main(lab, commit)
    found, within = gaps(summary)
    for g in within:
        log(f"발견 루프: {g['suite']}:{g['metric']} {g['actual']:.2f}는 목표 {g['final']:g}와의 차이가 측정 잡음 "
            f"{g['noise']:g} 안이라 건너뛴다")
    created, skipped = [], []
    for gap in found:
        if len(created) >= MAX_NEW_PER_RUN or len(open_auto) + len(created) >= MAX_OPEN_AUTO:
            break
        if any(marker(gap["suite"], gap["metric"]) in (i["description"] or "") for i in open_auto):
            skipped.append(gap["metric"])
            continue
        seen = observe(summary, gap)
        title = (f"[auto] {LABELS.get(gap['metric'], gap['metric'])} 개선: {SUITE_NAMES[gap['suite']]} "
                 f"{gap['actual']:.2f} → {gap['next']:g} (최종 목표 {gap['final']:g})")
        issue = linear.create_issue(title, issue_body(gap, summary, commit, eval_dir.name, seen),
                                    labels=["factory", "auto-discovered"])
        created.append({**issue, "gap": gap, "observe_cost_usd": seen.get("cost_usd")})
        log(f"발견 루프: {issue['identifier']} 생성 — {title}")
    if not found and not within:
        log(f"발견 루프: main {commit}은 최종 목표를 모두 충족한다. 이슈를 만들지 않는다")
    STATE_FILE.write_text(json.dumps({"commit": commit, "oracle": oracle, "eval_dir": eval_dir.name, "at": datetime.now().isoformat(),
                                      "gaps": found, "within_noise": within, "created": [c["identifier"] for c in created],
                                      "skipped": skipped}, indent=2, ensure_ascii=False))
    return created
