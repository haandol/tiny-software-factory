"""의도 → 작업 명세: Linear 이슈 본문과 사람 댓글을 실행 가능한 작업 명세로 바꾼다.

LLM은 자연어를 구조로 정리하는 일만 한다. 수용 조건은 프로그램이 정한다.
  - 기본 조건(실행, 계약 스모크, app-server 사용, 홀드아웃 회귀)은 항상 붙인다.
  - 지표 목표는 이슈 원문에 그 숫자가 실제로 적혀 있을 때만 받아들인다.
  - 부등호 방향은 최종 목표의 방향과 같아야 한다. 에이전트가 기준을 느슨하게 만드는 길을 막는다.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile

from . import harness
from .env import agent_env
from .linear import FACTORY_MARK
from .oracle import FINAL_TARGETS

DEFAULT_ACCEPTANCE = ["run", "contract-smoke", {"process": "codex app-server", "absent": "codex exec"}]
METRIC_KEYS = [k for k in FINAL_TARGETS if k != "viewer.console_errors"]

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["goal", "keep", "drop", "constraints", "notes", "metrics"],
    "properties": {
        "goal": {"type": "string"},
        "keep": {"type": "array", "items": {"type": "string"}},
        "drop": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
        "metrics": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["suite", "metric", "op", "value", "quote"],
            "properties": {"suite": {"enum": ["public", "holdout"]}, "metric": {"enum": METRIC_KEYS},
                           "op": {"enum": ["<=", ">="]}, "value": {"type": "number"}, "quote": {"type": "string"}}}},
    },
}


class SpecError(RuntimeError):
    """이슈를 작업 명세로 바꿀 수 없다. 사람에게 넘긴다."""


def human_comments(issue: dict) -> list[str]:
    return [c["body"] for c in issue["comments"] if not c["body"].startswith(FACTORY_MARK)]


def _ask(prompt: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(["claude", "-p", prompt, "--bare", "--output-format", "json",
                              "--json-schema", json.dumps(SCHEMA), "--max-budget-usd", "0.5"],
                             cwd=tmp, env=agent_env(), capture_output=True, text=True, timeout=300)
    try:
        result = json.loads(out.stdout)
    except ValueError:
        raise SpecError(f"작업 명세 생성 실패: {(out.stderr or out.stdout)[-300:]}")
    if result.get("is_error") or "structured_output" not in result:
        raise SpecError(f"작업 명세 생성 실패: {str(result.get('result'))[:300]}")
    return result["structured_output"] | {"_cost_usd": result.get("total_cost_usd")}


def _numbers(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]


def check_metrics(metrics: list[dict], source: str) -> dict[str, dict]:
    """지표 목표를 검증하고 suite별 acceptance 형식으로 바꾼다. 어기면 SpecError."""
    suites: dict[str, dict] = {}
    for m in metrics:
        op, _ = FINAL_TARGETS[m["metric"]]
        if m["op"] != op:
            raise SpecError(f"{m['metric']}의 방향은 '{op}'여야 한다 (받은 값 '{m['op']}')")
        if m["quote"] not in source or not any(abs(n - m["value"]) < 1e-9 for n in _numbers(m["quote"])):
            raise SpecError(f"{m['metric']} 목표 {m['value']}가 이슈 원문에 없다 (인용: {m['quote'][:80]})")
        suite = "holdout" if m["metric"].startswith("stability.") else m["suite"]   # 안정성 지표는 홀드아웃에만 있다
        suites.setdefault(suite, {})[m["metric"]] = f"{op} {m['value']:g}"
    return suites


def issue_to_work_item(issue: dict, target: str) -> dict:
    comments = human_comments(issue)
    metrics_doc = "\n".join(f"- {k} ({FINAL_TARGETS[k][0]}, 최종 목표 {FINAL_TARGETS[k][1]})" for k in METRIC_KEYS)
    prompt = harness.render("spec-task", identifier=issue["identifier"], title=issue["title"],
                            description=issue["description"] or "(없음)",
                            comments="\n---\n".join(comments) or "(없음)", metrics=metrics_doc)
    out = _ask(prompt)
    source = "\n".join([issue["title"], issue["description"], *comments])
    item = {"work_item": issue["identifier"], "target": target, "goal": out["goal"],
            "keep": out["keep"], "drop": out["drop"], "constraints": out["constraints"], "notes": out["notes"],
            "human_notes": comments, "source_issue": issue["url"], "_spec_cost_usd": out["_cost_usd"]}
    if target == "eval":   # 평가 자산 변경은 채점기 검증과 변경 범위로 판정한다
        item["acceptance"] = ["calibrate", "eval-scope"]
        return item
    suites = check_metrics(out["metrics"], source)
    item["acceptance"] = DEFAULT_ACCEPTANCE + [{"suite": s, "metrics": m} for s, m in suites.items()] + ["holdout-regression"]
    return item
