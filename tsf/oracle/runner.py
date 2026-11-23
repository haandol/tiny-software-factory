"""평가 실행: 장면을 앱에 요청하고, done을 기다리고, 뷰어를 측정해 채점한다."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import EVALS, ROOT
from .apps import AppHandle, http
from .code import measure_code
from .lab import Lab
from .policy import HOLDOUT_COUNT, IMAGE_H, IMAGE_W, PUBLIC_SEEDS, REQUEST_TIMEOUT_S, WORST_METRICS
from .scenes import generate_scene
from .scoring import compare, score_metrics
from .sheet import eval_sheet_html


@dataclass
class Fixture:
    id: str
    seed: int
    variant: str
    spec: dict = field(default_factory=dict)
    png: bytes = b""
    gt: dict = field(default_factory=dict)


def make_fixtures(suite: str, holdout_seeds: list[int] | None = None) -> list[Fixture]:
    out = []
    if suite in ("public", "all"):
        out += [Fixture(f"public-{i + 1:02d}", s, "public") for i, s in enumerate(PUBLIC_SEEDS)]
    if suite in ("holdout", "all"):
        seeds = holdout_seeds or [random.SystemRandom().randrange(10_000, 1_000_000) for _ in range(HOLDOUT_COUNT)]
        out += [Fixture(f"holdout-{s}", s, "holdout") for s in seeds]
    for fx in out:
        fx.spec = generate_scene(fx.seed, fx.variant)
    return out


def new_eval_dir(label: str) -> Path:
    d = EVALS / f"{datetime.now():%Y%m%d-%H%M%S}-{label}"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Violations(dict):
    """계약 스모크 위반: 항목 → 사례 목록."""

    def add(self, name: str, detail: str) -> None:
        self.setdefault(name, []).append(detail)


def _json_field(code: int, body: bytes, expected: int, key: str):
    try:
        return json.loads(body).get(key) if code == expected else None
    except (ValueError, AttributeError):
        return None


def _prepare(fixtures: list[Fixture], out_dir: Path, lab: Lab, registry: dict | None) -> None:
    for fx in fixtures:
        if not fx.png:
            lab.render(fx)
        if registry is not None:
            registry[hashlib.sha256(fx.png).hexdigest()] = fx.spec
        d = out_dir / fx.id
        d.mkdir(parents=True, exist_ok=True)
        (d / "input.png").write_bytes(fx.png)
        (d / "gt.json").write_text(json.dumps(fx.spec, indent=2))


def _submit(app: AppHandle, fixtures: list[Fixture], smoke: Violations) -> dict:
    submitted = {}
    for fx in fixtures:
        code, body = http("POST", f"{app.base_url}/api/requests", fx.png, "image/png")
        key = _json_field(code, body, 202, "key")
        if not key:
            smoke.add("post", f"{fx.id}: {code} {body[:120]!r}")
        submitted[fx.id] = {"key": key, "t0": time.time(), "status": "pending" if key else "failed"}
    return submitted


def _wait(app: AppHandle, submitted: dict, smoke: Violations, interval: float) -> None:
    deadline = time.time() + REQUEST_TIMEOUT_S
    while time.time() < deadline and any(s["status"] == "pending" for s in submitted.values()):
        for fx_id, s in submitted.items():
            if s["status"] != "pending":
                continue
            code, body = http("GET", f"{app.base_url}/api/requests/{s['key']}")
            status = _json_field(code, body, 200, "status")
            if status not in ("pending", "done", "failed"):
                smoke.add("status", f"{fx_id}: {code} {body[:120]!r}")
                s["status"] = "failed"
            elif status != "pending":
                s["status"], s["seconds"] = status, round(time.time() - s["t0"], 1)
                s["error"] = _json_field(code, body, 200, "error")
        time.sleep(interval)
    for s in submitted.values():
        if s["status"] == "pending":
            s["status"], s["error"] = "timeout", f"{REQUEST_TIMEOUT_S}초 안에 done이 되지 않았다"


def _measure(app: AppHandle, fx: Fixture, s: dict, out_dir: Path, lab: Lab, smoke: Violations) -> dict:
    """done이 된 장면 하나의 뷰어를 측정해 채점한다."""
    r = {"fixture": fx.id, "seed": fx.seed, "variant": fx.variant, "key": s["key"], "status": s["status"],
         "seconds": s.get("seconds"), "error": s.get("error"), "console_errors": 0,
         "metrics": dict(WORST_METRICS), "fidelity": 0.0}
    if s["status"] == "done":
        scene_file = app.data_dir / "scenes" / f"{s['key']}.js"
        if not scene_file.exists():
            smoke.add("scene_file", f"{fx.id}: {scene_file.name} 없음")
        elif "build" not in scene_file.read_text(errors="replace"):
            smoke.add("scene_file", f"{fx.id}: build 함수가 보이지 않는다")
        before = len(lab.external_requests)
        pred, n_err, err = lab.measure_viewer(f"{app.base_url}/view?key={s['key']}", out_dir / fx.id / "viewer.png")
        if len(lab.external_requests) > before:
            smoke.add("local_only", f"{fx.id}: {lab.external_requests[before]}")
        r["console_errors"] = n_err
        if pred is None:
            smoke.add("viewer", f"{fx.id}: {err}")
            r["error"] = err
        elif not pred["camera"]["isPerspective"]:
            smoke.add("viewer", f"{fx.id}: camera가 PerspectiveCamera가 아니다")
        elif not pred["roles"].get("ground"):
            smoke.add("ground_mesh", f"{fx.id}: role=ground 메시가 없다")
        else:
            r["metrics"] = compare(fx.gt, pred, fx.spec)
            r["fidelity"] = score_metrics(r["metrics"])
            (out_dir / fx.id / "measured.json").write_text(
                json.dumps({k: v for k, v in pred.items() if k != "mask"}, indent=2))
    r["metrics"]["viewer.console_errors"] = r["console_errors"]
    if s.get("seconds") is not None:
        r["metrics"]["run.seconds"] = s["seconds"]
    (out_dir / fx.id / "result.json").write_text(json.dumps(r, indent=2, ensure_ascii=False))
    return r


def run_eval(app: AppHandle, fixtures: list[Fixture], out_dir: Path, lab: Lab, registry: dict | None = None) -> dict:
    """fixtures를 app에 요청하고 측정·채점한다. 결과는 out_dir/summary.json과 sheet.png에도 남긴다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    smoke = Violations()
    _prepare(fixtures, out_dir, lab, registry)
    submitted = _submit(app, fixtures, smoke)
    _wait(app, submitted, smoke, interval=0.5 if registry is not None else 2)
    results = [_measure(app, fx, submitted[fx.id], out_dir, lab, smoke) for fx in fixtures]
    if not any(r["status"] == "done" for r in results):
        smoke.add("done", "done이 된 요청이 하나도 없다")
    summary = summarize(results)
    summary["smoke"] = {"passed": not smoke, "violations": dict(smoke)}
    if app.source_dir:
        summary["code"] = measure_code(app.source_dir)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    try:  # 사람이 한눈에 볼 수 있는 평가 시트. 실패해도 평가 결과에는 영향이 없다
        lab.screenshot_html(eval_sheet_html(summary, out_dir, out_dir.name), out_dir / "sheet.png")
    except Exception as e:
        print(f"  평가 시트를 만들지 못했다: {e}")
    return summary


def summarize(results: list[dict]) -> dict:
    def block(rows: list[dict]) -> dict:
        keys = sorted({k for r in rows for k in r["metrics"]})
        metrics = {}
        for k in keys:
            vals = [r["metrics"][k] for r in rows if k in r["metrics"]]
            metrics[k] = round(sum(vals) / len(vals), 4) if vals else None
        fidelity = round(sum(r["fidelity"] for r in rows) / len(rows), 2) if rows else None
        return {"count": len(rows), "fidelity": fidelity, "metrics": metrics}

    by_variant = {v: block([r for r in results if r["variant"] == v])
                  for v in ("public", "holdout") if any(r["variant"] == v for r in results)}
    return {"overall": block(results), **by_variant, "fixtures": results}


def print_summary(label: str, summary: dict) -> None:
    print(f"\n[{label}] smoke={'통과' if summary['smoke']['passed'] else '실패'}")
    for name, cases in summary["smoke"]["violations"].items():
        print(f"  계약 위반 {name}: {cases[0]}" + (f" 외 {len(cases) - 1}건" if len(cases) > 1 else ""))
    print(f"  {'fixture':<16}{'status':<9}{'fidelity':>9}{'fov°':>8}{'pitch°':>8}{'f1':>6}{'pos':>7}{'iou':>6}")
    for r in summary["fixtures"]:
        m = r["metrics"]
        print(f"  {r['fixture']:<16}{r['status']:<9}{r['fidelity']:>9.1f}{m['camera.fov_error_deg']:>8.1f}"
              f"{m['camera.pitch_error_deg']:>8.1f}{m['objects.f1']:>6.2f}{m['objects.position_error']:>7.3f}"
              f"{m['reprojection.iou']:>6.2f}")
    for v in ("public", "holdout"):
        if v in summary:
            print(f"  평균 {v:<8} fidelity {summary[v]['fidelity']:.1f}")
    if summary.get("code"):
        c = summary["code"]
        print(f"  코드 {c['code.files']}개 파일, {c['code.total_lines']}줄, 최대 파일 {c['code.max_file_lines']}줄, "
              f"최대 함수 {c['code.max_function_lines']}줄, 최대 복잡도 {c['code.max_complexity']}")


def eval_app(app_dir: Path, suite: str, label: str, holdout_seeds: list[int] | None = None) -> dict:
    """factory.py eval: 폴더의 app.py를 띄워 평가한다."""
    from .apps import start_product_app
    out = new_eval_dir(label)
    fixtures = make_fixtures(suite, holdout_seeds)
    app = start_product_app(app_dir, out / "data", out / "app.log")
    lab = Lab()
    try:
        summary = run_eval(app, fixtures, out, lab)
    finally:
        lab.close()
        app.stop()
    print_summary(label, summary)
    print(f"\n결과: {out.relative_to(ROOT)}/summary.json, sheet.png")
    return summary


