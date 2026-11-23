"""리팩터링 단계: eval을 통과한 구현을 병합하기 전에, 동작은 그대로 두고 구조만 정리한다.

코드 지표가 기준(oracle/policy.CODE_TARGETS) 안이면 건너뛴다. 기준을 넘으면 리팩터링 에이전트를 한 번
실행하고, 세 조건을 모두 만족할 때만 받아들인다.
  1. 작업 명세의 수용 조건을 다시 모두 통과한다 (동작이 그대로다)
  2. 공개 세트 충실도가 리팩터링 전보다 PUBLIC_TOLERANCE 넘게 내려가지 않는다
  3. 기준을 넘었던 지표가 줄고, 다른 지표는 늘지 않는다 (구조가 나아졌다)
받아들이지 않으면 리팩터링 전 커밋으로 되돌리고 그대로 병합한다. 구현은 이미 eval을 통과했기 때문이다.
"""

from __future__ import annotations

import json

from . import harness
from .env import git
from .oracle import CODE_TARGETS, PUBLIC_TOLERANCE, Lab, measure_code


def exceeded(code: dict) -> list[str]:
    return [k for k, limit in CODE_TARGETS.items() if code.get(k, 0) > limit]


def improved(before: dict, after: dict, over: list[str]) -> bool:
    no_worse = all(after[k] <= max(before[k], CODE_TARGETS[k]) for k in CODE_TARGETS)
    return no_worse and any(after[k] < before[k] for k in over)


def _metrics_table(code: dict) -> str:
    rows = [f"- {k}: {code.get(k)} (기준 {CODE_TARGETS[k]} 이하){'  ← 초과' if code.get(k, 0) > CODE_TARGETS[k] else ''}"
            for k in CODE_TARGETS]
    rows += [f"- 가장 긴 파일: {code.get('code.max_file')}", f"- 가장 긴 함수: {code.get('code.max_function')}",
             f"- 가장 복잡한 함수: {code.get('code.max_complexity_function')}",
             f"- 전체: 파일 {code['code.files']}개, {code['code.total_lines']}줄, 60줄 넘는 함수 {code['code.long_functions']}개"]
    return "\n".join(rows)


def refactor_stage(run, passing: dict, passing_commit: str, lab: Lab) -> dict:
    from .worker import commit_attempt, evaluate_stage, public_items, run_claude, self_eval_command
    before = measure_code(run.wt)
    over = exceeded(before)
    if not over:
        run.say("리팩터링 단계: 코드 지표가 기준 안이라 건너뛴다")
        return {"ran": False, "reason": "코드 지표가 기준 안", "code_before": before}

    run.say(f"리팩터링 단계: 기준 초과 {', '.join(over)} → Claude Code 실행")
    prompt = harness.render("refactor-task", work_item_id=run.work_id, metrics=_metrics_table(before),
                            public_before=passing["score"]["public"], tolerance=PUBLIC_TOLERANCE,
                            acceptance=json.dumps(run.item["acceptance"], ensure_ascii=False))
    system = harness.render("refactor-system", self_eval=self_eval_command(run.work_id))
    claude = run_claude(run.wt, prompt, system, run.dir / "refactor.jsonl")
    run.say(f"  Claude Code 끝: {claude['seconds']}초, ${claude['cost_usd'] or 0:.2f}, 도구 호출 {claude['tool_calls']}회")
    commit = commit_attempt(run.wt, f"{run.work_id} refactor")
    after = measure_code(run.wt)
    result = {"ran": True, "claude": claude, "commit": commit, "code_before": before, "code_after": after,
              "exceeded": over}
    if not commit:
        return result | {"accepted": False, "reason": "변경이 없었다"}

    run.say(f"  리팩터링 평가 시작 (커밋 {commit})")
    stage = public_items(evaluate_stage(run, "refactor", lab))
    public_now = stage["score"]["public"] or 0
    checks = {"behavior": stage["verdict"]["passed"],
              "fidelity": public_now >= (passing["score"]["public"] or 0) - PUBLIC_TOLERANCE,
              "structure": improved(before, after, over)}
    accepted = all(checks.values())
    if not accepted:
        git("reset", "-q", "--hard", passing_commit, cwd=run.wt)
    run.say(f"  리팩터링 {'수용' if accepted else '거부'}: " + ", ".join(f"{k}={'통과' if v else '실패'}"
                                                                    for k, v in checks.items()))
    return result | stage | {"checks": checks, "accepted": accepted,
                             "accepted_commit": commit if accepted else None}


def _last_passing_public() -> float | None:
    """가장 최근에 병합된 실행의 공개 세트 충실도. 리팩터링 전 기준으로 쓴다."""
    from .config import RUNS
    for path in sorted(RUNS.glob("*/run.json"), reverse=True):
        rec = json.loads(path.read_text())
        if rec.get("status") == "done":
            stage = rec["refactor"] if rec.get("refactor", {}).get("accepted") else rec["attempts"][-1]
            return stage["score"]["public"]
    return None


def refactor_main(lab: Lab) -> dict:
    """factory.py refactor: 이미 병합된 main에 리팩터링 단계만 실행한다.

    기존 코드베이스에 리팩터링 단계만 붙일 때도 같은 방식으로 쓴다. 수용 조건은 동작 보존뿐이다.
    """
    from datetime import datetime
    from .config import PRODUCT
    from .delivery import merge_and_deploy
    from .worker import Run, evaluate_stage, public_items, save_notes, write_context
    item = {"work_item": f"REFACTOR-{datetime.now():%m%d%H%M}", "target": "product",
            "goal": "동작을 바꾸지 않고 제품 코드의 구조를 정리한다",
            "acceptance": ["run", "contract-smoke", "holdout-regression"]}
    run = Run(item, print)
    write_context(run.wt, item, lab)
    public_before = _last_passing_public()
    if public_before is None:  # 기준이 없으면 리팩터링 전 코드를 먼저 평가한다
        public_before = public_items(evaluate_stage(run, "before", lab))["score"]["public"]
    head = git("rev-parse", "--short", "HEAD", cwd=run.wt)
    run.record["refactor"] = refactor_stage(run, {"score": {"public": public_before}}, head, lab)
    save_notes(run)
    if run.record["refactor"].get("accepted"):
        run.record["delivery"] = merge_and_deploy(item, run.wt, run.branch, run.record["refactor"]["accepted_commit"])
        run.record["status"] = "done" if run.record["delivery"]["deployed"] else "needs_human"
    else:
        run.record["status"] = "skipped" if not run.record["refactor"]["ran"] else "rejected"
        git("worktree", "remove", "--force", str(run.wt), cwd=PRODUCT)
    return run.finish()
