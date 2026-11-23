"""4단계: Linear를 제어판으로 쓰는 상주 실행 (factory.py up). 5단계부터 대기열이 비면 발견 루프가 돈다.

틱마다: 일시 정지 확인 → 취소된 이슈의 작업 공간 정리 → Todo 이슈 하나 선점 → 작업 명세 → 실행 → 결과 보고.
대기열이 비면: 발견 루프가 main을 평가해 최종 목표 미달 지표를 이슈로 만든다 (main 커밋마다 한 번).
상태의 원본은 Linear다. 로컬에는 실행 기록 파일만 있다 (Symphony SPEC §14.3과 같은 복구 방식).
  Todo         factory 또는 factory-eval 라벨이 붙으면 대기열
  In Progress  팩토리가 선점해 실행 중 (재시작하면 Todo로 되돌린다)
  Done         eval을 통과해 병합·배포됐다 (factory-eval은 calibrate 통과 후 팩토리에 병합)
  Needs Human  정해진 시도 안에 eval을 통과하지 못했다. 사람이 댓글을 달고 Todo로 되돌리거나 새 이슈를 만든다
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from . import linear
from .config import PRODUCT, ROOT, RUNS, VAR, WORKTREES
from .discovery import discover
from .env import git
from .eval_change import EVAL_WORKTREES, run_eval_change
from .linear import NEEDS_HUMAN
from .oracle import Lab
from .spec import SpecError, issue_to_work_item
from .worker import run_work_item, workspace_key

POLL_S = 30
LABEL_TARGETS = {"factory-eval": "eval", "factory": "product"}   # 앞의 것이 우선
DAILY_BUDGET_USD = 50
MAX_CONSECUTIVE_NEEDS_HUMAN = 3
PAUSE_FILE = VAR / "pause.json"
PAUSE_ACK = VAR / "pause-ack.json"       # 사람이 일시 정지 이슈를 Done으로 옮긴 기록


# --- 대기열 -------------------------------------------------------------------------

def target_of(issue: dict) -> str | None:
    return next((t for label, t in LABEL_TARGETS.items() if label in issue["labels"]), None)


def candidates() -> list[dict]:
    issues = [i for i in linear.fetch_issues(["Todo"]) if target_of(i)]
    return sorted(issues, key=lambda i: (i["priority"] if i["priority"] in (1, 2, 3, 4) else 9,
                                         i["created_at"] or "", i["identifier"]))


def recover() -> None:
    """재시작하면 실행 중이던 상태는 복원하지 않는다. In Progress 이슈를 Todo로 되돌려 다시 대기열에 넣는다."""
    for issue in linear.fetch_issues(["In Progress"]):
        if target_of(issue):
            linear.comment(issue, "팩토리가 다시 시작돼 실행이 끊겼다. 작업 공간은 남아 있으므로 대기열에 다시 넣는다.")
            linear.move(issue, "Todo")


def sweep_canceled() -> None:
    """Canceled로 옮겨진 이슈의 작업 공간을 지운다. 브랜치는 남긴다."""
    for issue in linear.fetch_issues(["Canceled"]):
        key = workspace_key(issue["identifier"])
        for root, repo in ((WORKTREES, PRODUCT), (EVAL_WORKTREES, ROOT)):
            if (root / key).exists():
                git("worktree", "remove", "--force", str(root / key), cwd=repo, check=False)
                shutil.rmtree(root / key, ignore_errors=True)
                print(f"[{issue['identifier']}] Canceled → 작업 공간 정리")


# --- 일시 정지: 비용 상한, 연속 Needs Human -------------------------------------------

def recent_runs() -> list[dict]:
    runs = [json.loads(p.read_text()) for p in RUNS.glob("*/run.json")]
    return sorted(runs, key=lambda r: r.get("started", ""))


def pause_reason() -> str | None:
    runs = recent_runs()
    today = datetime.now().date().isoformat()
    ack = json.loads(PAUSE_ACK.read_text()) if PAUSE_ACK.exists() else {}
    spent = sum(r.get("cost_usd") or 0 for r in runs if r.get("started", "").startswith(today))
    if spent >= DAILY_BUDGET_USD and ack.get("budget_date") != today:
        return f"오늘 Claude Code 비용 ${spent:.2f}가 하루 상한 ${DAILY_BUDGET_USD}에 닿았다"
    finished = [r for r in runs if r.get("status") in ("done", "needs_human")]
    tail = [r["status"] for r in finished[ack.get("runs_seen", 0):]][-MAX_CONSECUTIVE_NEEDS_HUMAN:]
    if len(tail) == MAX_CONSECUTIVE_NEEDS_HUMAN and all(s == "needs_human" for s in tail):
        return f"Needs Human이 연속 {MAX_CONSECUTIVE_NEEDS_HUMAN}건이다. eval 결함이나 실행 환경 문제일 수 있다"
    return None


def paused() -> bool:
    """일시 정지 중이면 True. 사람이 일시 정지 이슈를 Done으로 옮기면 풀린다."""
    if PAUSE_FILE.exists():
        pause = json.loads(PAUSE_FILE.read_text())
        issue = linear.fetch_issue(pause["id"])
        if issue and issue["state"] == "Done":
            PAUSE_FILE.unlink()
            finished = [r for r in recent_runs() if r.get("status") in ("done", "needs_human")]
            PAUSE_ACK.write_text(json.dumps({"budget_date": datetime.now().date().isoformat(),
                                             "runs_seen": len(finished), "resolved": pause["identifier"]}))
            print(f"일시 정지 해제 ({pause['identifier']}): 오늘은 비용 상한으로 다시 멈추지 않는다")
            return False
        return True
    reason = pause_reason()
    if reason:
        issue = linear.create_issue(f"[factory] 일시 정지: {reason[:60]}",
                                    f"{reason}.\n\n원인을 확인한 뒤 이 이슈를 Done으로 옮기면 팩토리가 다시 돈다.")
        PAUSE_FILE.write_text(json.dumps({"id": issue["id"], "identifier": issue["identifier"], "reason": reason}))
        print(f"일시 정지: {reason} → {issue['identifier']}")
        return True
    return False


# --- Linear 보고 --------------------------------------------------------------------

def conditions_table(conditions: list[dict]) -> str:
    rows = ["| 조건 | 결과 | 내용 |", "|---|---|---|"]
    for c in conditions:
        detail = " ".join(str(c["detail"]).split())[:160].replace("|", "\\|")
        rows.append(f"| `{c['name']}` | {'통과' if c['ok'] else '**실패**'} | {detail} |")
    return "\n".join(rows)


def score_line(entry: dict) -> str:
    sc, cl = entry.get("score") or {}, entry["claude"]
    parts = [f"공개 {sc.get('public')}", f"홀드아웃 {sc.get('holdout')}"]
    if sc.get("main_same_seeds") is not None:
        parts.append(f"같은 시드 main {sc['main_same_seeds']}")
    parts.append(f"Claude Code {cl['seconds']}초 ${cl['cost_usd'] or 0:.2f}")
    return " · ".join(parts)


def sheet_markdown(eval_dir: str | None) -> str:
    if not eval_dir:
        return ""
    url = linear.upload(Path(eval_dir) / "candidate" / "sheet.png")
    return f"\n\n![평가 시트]({url})" if url else ""


def report_attempt(issue: dict, entry: dict) -> None:
    v = entry["verdict"]
    head = f"시도 {entry['attempt']}: eval {'통과' if v['passed'] else '실패'} (커밋 `{entry['commit']}`)"
    body = conditions_table(v["conditions"])
    if "score" in entry:
        body += f"\n\n{score_line(entry)}"
    linear.comment(issue, f"{head}\n\n{body}")


NEXT_ACTIONS = """다음 행동 (Linear에서 고른다)
- 구현 방향이 틀렸다 → 힌트를 댓글로 달고 이 이슈를 Todo로 옮긴다. 보존한 브랜치에서 이어서 실행한다.
- 요구사항이 모호하거나 너무 크다 → 나누거나 다시 쓴 새 이슈를 만들고 이 이슈는 Canceled로 옮긴다.
- eval이 틀렸다 → `factory-eval` 라벨로 이슈를 만든다. 평가 자산을 고친 뒤 이 이슈를 다시 Todo로 옮긴다."""


def report_final(issue: dict, record: dict) -> None:
    status = record["status"]
    if status == "done" and record.get("target") == "eval":
        linear.comment(issue, f"완료: 평가 자산 변경이 calibrate를 통과해 팩토리 main `{record['delivery']['merged']}`에"
                              " 병합됐다. 팩토리는 새 평가 코드로 다시 시작한다.")
        linear.move(issue, "Done")
    elif status == "done":
        last = record["refactor"] if record.get("refactor", {}).get("accepted") else record["attempts"][-1]
        rf = record.get("refactor") or {}
        rf_text = ("수용" if rf.get("accepted") else "거부, 리팩터링 전 코드로 병합" if rf.get("ran")
                   else f"건너뜀 ({rf.get('reason', '')})")
        linear.comment(issue, f"완료: eval 통과 → main `{record['delivery']['merged']}` 병합, "
                              f"{record['delivery']['url']} 배포\n\n{score_line(last)}\n\n리팩터링 단계: {rf_text}\n\n"
                              f"합계: Claude Code ${record.get('cost_usd', 0):.2f}, Codex {record.get('codex_calls', 0)}회"
                              f"{sheet_markdown(last.get('eval_dir'))}")
        linear.move(issue, "Done")
    elif status == "needs_human":
        attempts = record["attempts"]
        best = next((a for a in attempts if a["attempt"] == record.get("best_attempt")), attempts[-1] if attempts else None)
        notes_path = RUNS / record["run_id"] / "attempt-notes.md"
        notes = notes_path.read_text()[-1200:] if notes_path.exists() else "(없음)"
        best_text = (f"최고점 시도 {best['attempt']} (커밋 `{best['commit']}`, 브랜치 `{record.get('branch')}`)\n\n"
                     f"{conditions_table(best['verdict']['conditions'])}" if best else "")
        linear.comment(issue, f"eval을 통과하지 못해 병합하지 않았다 (시도 {len(attempts)}회).\n\n{best_text}\n\n"
                              f"에이전트의 원인 추정\n\n> {notes.strip().replace(chr(10), chr(10) + '> ')}\n\n{NEXT_ACTIONS}"
                              f"{sheet_markdown(best.get('eval_dir') if best else None)}")
        linear.move(issue, NEEDS_HUMAN)


# --- 이슈 하나 처리 -----------------------------------------------------------------

def process(issue: dict, lab: Lab) -> bool:
    """이슈 하나를 처리한다. 평가 자산이 바뀌어 다시 시작해야 하면 True."""
    target = target_of(issue)
    linear.move(issue, "In Progress")
    linear.comment(issue, f"실행 시작: 대상 `{target}`, 작업 공간 `{workspace_key(issue['identifier'])}`")
    try:
        item = issue_to_work_item(issue, target)
    except SpecError as e:
        linear.comment(issue, f"이슈를 작업 명세로 바꾸지 못해 실행하지 않았다.\n\n> {e}\n\n{NEXT_ACTIONS}")
        linear.move(issue, NEEDS_HUMAN)
        return False
    shown = {k: v for k, v in item.items() if not k.startswith("_") and k != "human_notes"}
    linear.comment(issue, f"작업 명세\n\n```yaml\n{yaml.safe_dump(shown, allow_unicode=True, sort_keys=False)}```")

    def still_active() -> bool:
        current = linear.fetch_issue(issue["id"])
        return bool(current) and current["state"] == "In Progress"

    def on_attempt(entry: dict) -> None:
        try:
            report_attempt(issue, entry)
        except Exception as e:  # 보고 실패는 실행을 멈추지 않는다
            print(f"  Linear 보고 실패: {e}")

    if target == "eval":
        record = run_eval_change(item, on_attempt=on_attempt, should_continue=still_active)
    else:
        record = run_work_item(item, lab, on_attempt=on_attempt, should_continue=still_active)
    record["target"] = target
    report_final(issue, record)
    return bool(record.get("reload"))


def up(once: bool = False, interval: int = POLL_S) -> None:
    """factory.py up: Linear를 폴링하며 이슈를 하나씩 처리한다. once면 대기열이 빌 때 끝낸다."""
    recover()
    lab = Lab()
    try:
        while True:
            try:
                sweep_canceled()
                issue = None if paused() else next(iter(candidates()), None)
            except Exception as e:  # 트래커 오류는 이번 틱만 건너뛴다 (Symphony SPEC §11.4)
                print(f"Linear 조회 실패, 다음 틱에 다시 시도: {e}")
                issue = None
            if issue is None and not paused() and discover(lab):   # 대기열이 비면 발견 루프가 다음 일을 찾는다
                continue
            if issue is None:
                if once:
                    return
                time.sleep(interval)
                continue
            print(f"[{issue['identifier']}] 선점: {issue['title']}")
            if process(issue, lab):
                print("평가 자산이 바뀌었다. 새 코드로 다시 시작한다.")
                lab.close()
                os.execv(sys.executable, [sys.executable, str(ROOT / "factory.py"), *sys.argv[1:]])
    finally:
        try:
            lab.close()
        except Exception:
            pass
