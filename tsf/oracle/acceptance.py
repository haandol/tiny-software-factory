"""수용 조건 판정: 작업 명세의 acceptance를 하나씩 판정한다. 모두 통과해야 eval 통과다."""

from __future__ import annotations

from .policy import REGRESSION_TOLERANCE


def parse_threshold(expr: str) -> tuple[str, float]:
    expr = str(expr).strip()
    op = expr[:2]
    if op not in ("<=", ">="):
        raise RuntimeError(f"목표값은 '<= 숫자' 또는 '>= 숫자' 형식이어야 한다: {expr}")
    return op, float(expr[2:])


def meets(op: str, actual: float | None, threshold: float) -> bool:
    if actual is None:
        return False
    return actual <= threshold if op == "<=" else actual >= threshold


class Verdict:
    def __init__(self):
        self.conditions: list[dict] = []
        self.failures: list[dict] = []

    def cond(self, name: str, ok: bool, detail: str) -> None:
        self.conditions.append({"name": name, "ok": ok, "detail": detail})

    def as_dict(self) -> dict:
        return {"passed": all(c["ok"] for c in self.conditions), "conditions": self.conditions,
                "failures": self.failures}


def _smoke(v: Verdict, candidate: dict | None) -> None:
    if candidate is None:
        return v.cond("contract-smoke", False, "앱이 시작되지 않아 검사하지 못했다")
    viol = candidate["smoke"]["violations"]
    v.cond("contract-smoke", not viol, "계약 위반 없음" if not viol else "; ".join(f"{k}: {c[0]}" for k, c in viol.items()))


def _regression(v: Verdict, candidate: dict | None, baseline: dict | None) -> None:
    if baseline is None:
        return v.cond("holdout-regression", True, "비교할 main이 없어 생략했다")
    if candidate is None or "holdout" not in candidate:
        return v.cond("holdout-regression", False, "후보의 홀드아웃 점수가 없다")
    c, b = candidate["holdout"]["fidelity"], baseline["holdout"]["fidelity"]
    v.cond("holdout-regression", c >= b - REGRESSION_TOLERANCE,
           f"후보 {c:.1f} / main {b:.1f} (같은 시드, 허용폭 {REGRESSION_TOLERANCE})")


def _worst(candidate: dict, suite: str, metric: str, op: str) -> list[dict]:
    rows = [r for r in candidate["fixtures"] if suite in ("all", "overall") or r["variant"] == suite]
    rows.sort(key=lambda r: r["metrics"].get(metric, 0), reverse=(op == "<="))
    return [{"fixture": r["fixture"], "seed": r["seed"], "value": r["metrics"].get(metric),
             "status": r["status"], "error": r["error"]} for r in rows[:3]]


def _suite(v: Verdict, raw: dict, candidate: dict | None) -> None:
    suite = raw["suite"]
    block = (candidate or {}).get(suite if suite in ("public", "holdout") else "overall")
    for metric, expr in raw.get("metrics", {}).items():
        op, threshold = parse_threshold(expr)
        actual = block["metrics"].get(metric) if block else None
        ok = meets(op, actual, threshold)
        v.cond(f"{suite}:{metric}", ok, f"{actual} (목표 {op} {threshold})")
        if not ok and candidate:
            v.failures.append({"metric": metric, "suite": suite, "expected": f"{op} {threshold}", "actual": actual,
                               "worst_fixtures": _worst(candidate, suite, metric, op)})


def judge(item: dict, candidate: dict | None, baseline: dict | None, start_error: str | None) -> dict:
    v = Verdict()
    for raw in item["acceptance"]:
        if raw == "run":
            v.cond("run", start_error is None, start_error or "app.py가 계약대로 시작됐다")
        elif raw == "contract-smoke":
            _smoke(v, candidate)
        elif raw == "holdout-regression":
            _regression(v, candidate, baseline)
        elif isinstance(raw, dict) and "suite" in raw:
            _suite(v, raw, candidate)
        else:
            raise RuntimeError(f"알 수 없는 수용 조건: {raw}")
    return v.as_dict()
