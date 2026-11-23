"""factory-eval 이슈: 평가 자산(tsf/oracle/, CONTRACT.md)을 고치는 경로.

제품 작업과 같은 생산 루프를 쓰지만 대상과 수용 조건이 다르다.
  대상      팩토리 저장소의 worktree (제품 저장소가 아니다)
  수용 조건 1. 바뀐 파일이 EVAL_SCOPE 안에만 있다  2. worktree의 새 코드로 돌린 calibrate가 통과한다
통과하면 팩토리 main에 squash 병합한다. 실행 중인 up 프로세스는 새 평가 코드로 다시 시작해야 한다.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import harness
from .config import MAX_ATTEMPTS, ROOT, RUNS, VAR, VENDOR
from .env import agent_env, git

EVAL_SCOPE = ("tsf/oracle/", "CONTRACT.md")
EVAL_WORKTREES = VAR / "worktrees-factory"


def key(work_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", work_id)


def prepare(work_id: str) -> tuple[Path, str]:
    branch, path = f"factory-eval/{key(work_id)}", EVAL_WORKTREES / key(work_id)
    exclude = ROOT / ".git" / "info" / "exclude"   # 작업 공간 자료와 vendor 링크는 커밋하지 않는다
    current = exclude.read_text() if exclude.exists() else ""
    missing = [p for p in (".factory/", "/vendor") if p not in current.split()]
    if missing:
        with open(exclude, "a") as f:
            f.write("\n" + "\n".join(missing) + "\n")
    if not path.exists():
        EVAL_WORKTREES.mkdir(parents=True, exist_ok=True)
        exists = git("branch", "--list", branch, cwd=ROOT)
        git("worktree", "add", str(path), *([branch] if exists else ["-b", branch, "main"]), cwd=ROOT)
    if not (path / "vendor").exists():          # vendor/는 git 밖이라 링크로 넣는다
        (path / "vendor").symlink_to(VENDOR)
    return path, branch


def changed_files(wt: Path) -> list[str]:
    base = git("merge-base", "HEAD", "main", cwd=wt)
    return [f for f in git("diff", "--name-only", base, "HEAD", cwd=wt).splitlines() if f]


def run_calibrate(wt: Path, log_path: Path) -> tuple[bool, str]:
    """worktree의 코드로 채점기 검증을 돌린다. (통과 여부, 출력 끝부분)"""
    out = subprocess.run(["uv", "run", "--quiet", "factory.py", "calibrate"], cwd=wt, env=agent_env(),
                         capture_output=True, text=True, timeout=1800)
    log_path.write_text(out.stdout + out.stderr)
    tail = "\n".join((out.stdout + out.stderr).splitlines()[-16:])
    return out.returncode == 0, tail


def judge(wt: Path, run_dir: Path, attempt: int) -> dict:
    files = changed_files(wt)
    outside = [f for f in files if not f.startswith(EVAL_SCOPE)]
    conditions = [{"name": "eval-scope", "ok": bool(files) and not outside,
                   "detail": ("바뀐 파일 없음" if not files else
                              f"범위 밖 변경: {', '.join(outside)}" if outside else f"{', '.join(files)}")}]
    if conditions[0]["ok"]:
        ok, tail = run_calibrate(wt, run_dir / f"attempt-{attempt}-calibrate.log")
        conditions.append({"name": "calibrate", "ok": ok, "detail": tail})
    return {"passed": all(c["ok"] for c in conditions), "conditions": conditions, "files": files}


def merge(item: dict, wt: Path, branch: str) -> str:
    dirty = git("status", "--porcelain", "--", *EVAL_SCOPE, cwd=ROOT)
    if dirty:
        raise RuntimeError(f"팩토리 저장소의 평가 자산에 커밋하지 않은 변경이 있어 병합하지 않는다: {dirty[:200]}")
    git("merge", "-q", "--squash", branch, cwd=ROOT)
    goal = " ".join(str(item["goal"]).split())[:72]
    git("commit", "-q", "-m", f"{item['work_item']}: {goal}\n\nfactory-eval, calibrate 통과", cwd=ROOT, identity=True)
    git("worktree", "remove", "--force", str(wt), cwd=ROOT)
    return git("rev-parse", "--short", "HEAD", cwd=ROOT)


def run_eval_change(item: dict, log: Callable[[str], None] = print, on_attempt=None, should_continue=None) -> dict:
    import yaml
    from .worker import commit_attempt, run_claude
    work_id = str(item["work_item"])
    run_dir = RUNS / f"{key(work_id)}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True)
    wt, branch = prepare(work_id)
    (wt / ".factory").mkdir(exist_ok=True)
    (wt / ".factory" / "work-item.yaml").write_text(yaml.safe_dump(item, allow_unicode=True, sort_keys=False))
    record = {"run_id": run_dir.name, "work_item": work_id, "target": "eval", "branch": branch, "attempts": [],
              "status": "running", "started": datetime.now().isoformat()}
    system = harness.render("eval-system")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if should_continue and not should_continue():
            record["status"] = "canceled"
            break
        hint = ("이전 시도가 수용되지 않았다. .factory/feedback.json을 먼저 읽어라." if attempt > 1 else "첫 시도다.")
        prompt = harness.render("eval-task", work_item_id=work_id, attempt=attempt, max_attempts=MAX_ATTEMPTS,
                                work_item_yaml=yaml.safe_dump(item, allow_unicode=True, sort_keys=False),
                                feedback_hint=hint)
        log(f"[{work_id}] 평가 자산 시도 {attempt}/{MAX_ATTEMPTS}: Claude Code 실행")
        claude = run_claude(wt, prompt, system, run_dir / f"attempt-{attempt}.jsonl")
        commit = commit_attempt(wt, f"{work_id} eval attempt {attempt}")
        verdict = judge(wt, run_dir, attempt)
        notes = wt / ".factory" / "attempt-notes.md"
        if notes.exists():  # 병합하면 worktree가 지워지므로 먼저 옮긴다
            shutil.copy(notes, run_dir / "attempt-notes.md")
        entry = {"attempt": attempt, "commit": commit, "claude": claude, "verdict": verdict}
        record["attempts"].append(entry)
        for c in verdict["conditions"]:
            log(f"[{work_id}]   {'통과' if c['ok'] else '실패'}  {c['name']}: {c['detail'].splitlines()[-1][:160]}")
        if on_attempt:
            on_attempt(entry)
        if verdict["passed"]:
            record["delivery"] = {"merged": merge(item, wt, branch), "repo": "factory"}
            record["status"], record["reload"] = "done", True
            break
        (wt / ".factory" / "feedback.json").write_text(json.dumps(
            {"attempt": attempt, "conditions": verdict["conditions"], "files": verdict["files"]},
            indent=2, ensure_ascii=False))
    else:
        record["status"] = "needs_human"
    record["finished"] = datetime.now().isoformat()
    record["cost_usd"] = round(sum(a["claude"]["cost_usd"] or 0 for a in record["attempts"]), 4)
    (run_dir / "run.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))
    log(f"[{work_id}] 결과: {record['status']} (Claude Code ${record['cost_usd']:.2f}) → {run_dir.relative_to(ROOT)}/run.json")
    return record
