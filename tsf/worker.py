"""생산 루프: 작업 명세 하나를 구현 → 평가 → (재시도) → 리팩터링 → 병합까지 끌고 간다.

eval을 통과하지 못하면 실패 증거를 다음 시도에 넘기고, 시도를 다 쓰면 병합하지 않고 사람에게 넘긴다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import harness
from .config import (CLAUDE_BUDGET_PER_ATTEMPT_USD, CLAUDE_TIMEOUT_S, HARNESS_VERSION, MAX_ATTEMPTS, PRODUCT,
                     ROOT, RUNS, VENDOR, WORKTREES)
from .delivery import merge_and_deploy
from .env import agent_env, git
from .oracle import Lab, judge, make_fixtures, run_eval, start_product_app


# --- 작업 명세와 작업 공간 ----------------------------------------------------------

EXCLUDE_PATTERNS = [".factory/", "__pycache__/", "*.pyc"]

def load_work_item(path: Path) -> dict:
    import yaml
    item = yaml.safe_load(path.read_text())
    for key in ("work_item", "goal", "acceptance"):
        if key not in item:
            raise RuntimeError(f"작업 명세에 {key}가 없다: {path}")
    return item


def prepare_worktree(work_id: str) -> tuple[Path, str]:
    """이슈별 브랜치와 worktree. 이미 있으면 이어서 쓴다."""
    branch, path = f"factory/{work_id}", WORKTREES / work_id
    exclude = PRODUCT / ".git" / "info" / "exclude"   # 팩토리가 시도마다 git add -A로 커밋하므로 미리 뺀다
    current = exclude.read_text() if exclude.exists() else ""
    missing = [p for p in EXCLUDE_PATTERNS if p not in current.split()]
    if missing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a") as f:
            f.write("\n" + "\n".join(missing) + "\n")
    if path.exists():
        return path, branch
    WORKTREES.mkdir(parents=True, exist_ok=True)
    exists = git("branch", "--list", branch, cwd=PRODUCT)
    git("worktree", "add", str(path), *([branch] if exists else ["-b", branch, "main"]), cwd=PRODUCT)
    return path, branch


def write_context(wt: Path, item: dict, lab: Lab) -> None:
    """작업 공간의 .factory/: 작업 명세, 계약, 공개 세트(이미지와 정답), three.js 사본."""
    import yaml
    ctx = wt / ".factory"
    (ctx / "fixtures").mkdir(parents=True, exist_ok=True)
    (ctx / "vendor").mkdir(exist_ok=True)
    (ctx / "work-item.yaml").write_text(yaml.safe_dump(item, allow_unicode=True, sort_keys=False))
    shutil.copy(ROOT / "CONTRACT.md", ctx / "CONTRACT.md")
    for f in ("three.module.js", "three.core.js", "LICENSE"):
        shutil.copy(VENDOR / f, ctx / "vendor" / f)
    for fx in make_fixtures("public"):
        d = ctx / "fixtures" / fx.id
        if (d / "input.png").exists():
            continue
        lab.render(fx)
        d.mkdir(exist_ok=True)
        (d / "input.png").write_bytes(fx.png)
        (d / "gt.json").write_text(json.dumps(fx.spec, indent=2))


def commit_attempt(wt: Path, message: str) -> str | None:
    git("add", "-A", cwd=wt)
    if not git("status", "--porcelain", cwd=wt):
        return None
    git("commit", "-q", "-m", message, cwd=wt, identity=True)
    return git("rev-parse", "--short", "HEAD", cwd=wt)


def self_eval_command(work_id: str) -> str:
    return f"uv run {ROOT / 'factory.py'} eval --app . --suite public --label self-{work_id}"


# --- 에이전트 실행 ------------------------------------------------------------------

def _read_events(log_path: Path) -> list[dict]:
    events = []
    for line in log_path.read_text(errors="replace").splitlines():
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events


def run_claude(wt: Path, prompt: str, system: str, log_path: Path) -> dict:
    """Claude Code를 비대화식으로 실행한다. Bedrock default profile, 모델 지정 없음, 개인 설정 없음."""
    cmd = ["claude", "-p", prompt, "--bare", "--append-system-prompt", system,
           "--output-format", "stream-json", "--verbose",
           "--dangerously-skip-permissions", "--max-budget-usd", str(CLAUDE_BUDGET_PER_ATTEMPT_USD)]
    started = time.time()
    with open(log_path, "w") as log:
        try:
            code = subprocess.run(cmd, cwd=wt, env=agent_env(), stdout=log, stderr=subprocess.STDOUT,
                                  timeout=CLAUDE_TIMEOUT_S).returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    events = _read_events(log_path)
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    tool_calls = sum(1 for e in events if e.get("type") == "assistant"
                     for c in e.get("message", {}).get("content", []) if c.get("type") == "tool_use")
    return {"exit": code, "model": init.get("model"), "cost_usd": result.get("total_cost_usd"),
            "turns": result.get("num_turns"), "tool_calls": tool_calls, "seconds": round(time.time() - started),
            "subtype": result.get("subtype"), "is_error": result.get("is_error"),
            "summary": str(result.get("result", ""))[-1500:]}


# --- 평가와 피드백 ------------------------------------------------------------------

def evaluate_candidate(wt: Path, eval_dir: Path, lab: Lab) -> tuple[dict | None, dict | None, str | None]:
    """후보와 현재 main을 같은 홀드아웃 시드로 평가한다. (후보 요약, main 요약, 시작 오류)"""
    fixtures = make_fixtures("all")
    holdout = [fx for fx in fixtures if fx.variant == "holdout"]
    candidate = baseline = None
    try:
        app = start_product_app(wt, eval_dir / "candidate" / "data", eval_dir / "candidate-app.log")
    except RuntimeError as e:
        return None, None, str(e)
    try:
        candidate = run_eval(app, fixtures, eval_dir / "candidate", lab)
    finally:
        app.stop()
    if (PRODUCT / "app.py").exists():
        try:
            main_app = start_product_app(PRODUCT, eval_dir / "main" / "data", eval_dir / "main-app.log")
        except RuntimeError as e:
            print(f"  main을 띄우지 못해 회귀 비교를 생략한다: {e}")
        else:
            try:
                baseline = run_eval(main_app, holdout, eval_dir / "main", lab)
            finally:
                main_app.stop()
    return candidate, baseline, None


def scores(candidate: dict | None, baseline: dict | None) -> dict:
    return {"public": (candidate or {}).get("public", {}).get("fidelity"),
            "holdout": (candidate or {}).get("holdout", {}).get("fidelity"),
            "main_same_seeds": (baseline or {}).get("holdout", {}).get("fidelity")}


def write_feedback(wt: Path, attempt: int, verdict: dict, candidate: dict | None, baseline: dict | None,
                   eval_dir: Path) -> None:
    """실패 증거를 다음 시도의 .factory/feedback.json으로 넘긴다. 공개 세트만 이미지를 함께 준다."""
    ctx, fb_dir = wt / ".factory", wt / ".factory" / "feedback"
    shutil.rmtree(fb_dir, ignore_errors=True)
    fb_dir.mkdir(parents=True)
    for f in verdict["failures"]:
        for w in f["worst_fixtures"]:
            src = eval_dir / "candidate" / w["fixture"] / "viewer.png"
            if w["fixture"].startswith("public") and src.exists():
                shutil.copy(src, fb_dir / f"{w['fixture']}-viewer.png")
                w["evidence"] = [f".factory/fixtures/{w['fixture']}/input.png",
                                 f".factory/feedback/{w['fixture']}-viewer.png"]
    if (eval_dir / "candidate" / "sheet.png").exists():
        shutil.copy(eval_dir / "candidate" / "sheet.png", fb_dir / "sheet.png")
    feedback = {"attempt": attempt, "verdict": "failed", "conditions": verdict["conditions"],
                "failures": verdict["failures"], "score": scores(candidate, baseline),
                "smoke_violations": (candidate or {}).get("smoke", {}).get("violations", {}),
                "sheet": ".factory/feedback/sheet.png"}
    app_log = eval_dir / "candidate-app.log"
    if app_log.exists():
        (fb_dir / "app.log.tail").write_text(app_log.read_text(errors="replace")[-4000:])
        feedback["app_log"] = ".factory/feedback/app.log.tail"
    notes = ctx / "attempt-notes.md"
    if notes.exists():
        feedback["previous_notes"] = notes.read_text()[-3000:]
    (ctx / "feedback.json").write_text(json.dumps(feedback, indent=2, ensure_ascii=False))


# --- 실행 하나 ----------------------------------------------------------------------

class Run:
    """실행 하나의 기록. var/runs/<실행 ID>/run.json에 남는다."""

    def __init__(self, item: dict, log: Callable[[str], None]):
        self.item, self.log = item, log
        self.work_id = str(item["work_item"])
        self.dir = RUNS / f"{self.work_id}-{datetime.now():%Y%m%d-%H%M%S}"
        self.dir.mkdir(parents=True)
        self.wt, self.branch = prepare_worktree(self.work_id)
        self.record = {"run_id": self.dir.name, "work_item": self.work_id, "branch": self.branch,
                       "worktree": str(self.wt), "harness": HARNESS_VERSION, "attempts": [],
                       "status": "running", "started": datetime.now().isoformat()}

    def say(self, msg: str) -> None:
        self.log(f"[{self.work_id}] {msg}")

    def save(self) -> None:
        (self.dir / "run.json").write_text(json.dumps(self.record, indent=2, ensure_ascii=False))

    def finish(self) -> dict:
        rec = self.record
        rec["finished"] = datetime.now().isoformat()
        stages = rec["attempts"] + ([rec["refactor"]] if rec.get("refactor", {}).get("claude") else [])
        rec["cost_usd"] = round(sum(s["claude"]["cost_usd"] or 0 for s in stages), 4)
        rec["codex_calls"] = sum(s.get("codex_calls", 0) for s in stages)
        self.save()
        self.say(f"결과: {rec['status']} (Claude Code ${rec['cost_usd']:.2f}, Codex 호출 {rec['codex_calls']}회)"
                 f" → {self.dir.relative_to(ROOT)}/run.json")
        return rec


def evaluate_stage(run: Run, label: str, lab: Lab) -> dict:
    """현재 worktree를 평가하고 작업 명세의 수용 조건으로 판정한다."""
    eval_dir = run.dir / f"{label}-eval"
    candidate, baseline, start_error = evaluate_candidate(run.wt, eval_dir, lab)
    verdict = judge(run.item, candidate, baseline, start_error)
    for c in verdict["conditions"]:
        run.say(f"  {'통과' if c['ok'] else '실패'}  {c['name']}: {c['detail']}")
    return {"eval_dir": str(eval_dir), "verdict": verdict, "score": scores(candidate, baseline),
            "code": (candidate or {}).get("code"),
            "codex_calls": sum(len(s["fixtures"]) for s in (candidate, baseline) if s),
            "generation_seconds": [r["seconds"] for s in (candidate, baseline) if s
                                   for r in s["fixtures"] if r.get("seconds") is not None],
            "_candidate": candidate, "_baseline": baseline}


def implement_attempt(run: Run, attempt: int, lab: Lab) -> dict:
    import yaml
    feedback = run.wt / ".factory" / "feedback.json"
    hint = ("이전 시도가 eval을 통과하지 못했다. .factory/feedback.json과 .factory/feedback/을 먼저 읽어라."
            if feedback.exists() else "첫 시도다.")
    prompt = harness.render("implement-task", work_item_id=run.work_id,
                            work_item_yaml=yaml.safe_dump(run.item, allow_unicode=True, sort_keys=False),
                            attempt=attempt, max_attempts=MAX_ATTEMPTS, feedback_hint=hint)
    system = harness.render("implement-system", self_eval=self_eval_command(run.work_id))
    run.say(f"시도 {attempt}/{MAX_ATTEMPTS}: Claude Code 실행")
    claude = run_claude(run.wt, prompt, system, run.dir / f"attempt-{attempt}.jsonl")
    run.say(f"  Claude Code 끝: {claude['seconds']}초, ${claude['cost_usd'] or 0:.2f}, 도구 호출 {claude['tool_calls']}회")
    commit = commit_attempt(run.wt, f"{run.work_id} attempt {attempt}")
    run.say(f"  평가 시작 (커밋 {commit})")
    stage = evaluate_stage(run, f"attempt-{attempt}", lab)
    return {"attempt": attempt, "commit": commit, "claude": claude, **stage}


def save_notes(run: "Run") -> None:
    """에이전트가 .factory/에 남긴 노트를 실행 폴더로 옮긴다. 병합 뒤 작업 공간은 지워지기 때문이다."""
    for note in (run.wt / ".factory").glob("*notes.md"):
        shutil.copy(note, run.dir / note.name)


def public_items(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def run_work_item(item: dict, lab: Lab, log: Callable[[str], None] = print) -> dict:
    """작업 명세 하나를 끝까지 실행한다: 구현 → 평가 → (재시도) → 리팩터링 → 병합·배포."""
    from .refactor import refactor_stage
    run = Run(item, log)
    write_context(run.wt, item, lab)
    best = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        entry = implement_attempt(run, attempt, lab)
        run.record["attempts"].append(public_items(entry))
        run.save()
        if entry["commit"] and (best is None or (entry["score"]["public"] or 0) > (best["score"]["public"] or 0)):
            best = entry
        if entry["verdict"]["passed"]:
            commit = entry["commit"] or git("rev-parse", "--short", "HEAD", cwd=run.wt)
            run.record["refactor"] = refactor_stage(run, entry, commit, lab)
            final = run.record["refactor"].get("accepted_commit") or commit
            run.say(f"eval 통과 → 병합과 배포 (커밋 {final})")
            save_notes(run)
            run.record["delivery"] = merge_and_deploy(item, run.wt, run.branch, final)
            run.record["status"] = "done" if run.record["delivery"]["deployed"] else "needs_human"
            break
        write_feedback(run.wt, attempt, entry["verdict"], entry["_candidate"], entry["_baseline"],
                       Path(entry["eval_dir"]))
    else:
        save_notes(run)
        run.record["status"] = "needs_human"
        run.record["best_attempt"] = best["attempt"] if best else None
        if best and best["commit"]:  # 최고점 시도를 브랜치 끝에 남겨 사람이 볼 수 있게 한다
            git("reset", "-q", "--hard", best["commit"], cwd=run.wt)
    return run.finish()
