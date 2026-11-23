"""평가 실행: 장면을 앱에 요청하고, done을 기다리고, 뷰어를 측정해 채점한다.

안정성 (SWF-5): STABILITY_VARIANTS의 장면은 바이트만 다른 이미지로 한 번 더 요청한다 (두 번째 요청).
두 결과를 각각 채점하고, 첫 요청 행의 metrics에 두 결과의 차이(stability.*)를 붙인다.
충실도와 다른 지표, 회귀 비교는 첫 요청의 결과만 쓴다. 두 번째 요청의 결과는 행의 "repeat"에 남는다.
"""

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
from .processes import process_group_commands
from .policy import (HOLDOUT_COUNT, PUBLIC_SEEDS, REQUEST_TIMEOUT_S, STABILITY_VARIANTS, STABILITY_WORST_FOV_DELTA,
                     WORST_METRICS)
from .scenes import generate_scene
from .scoring import compare, score_metrics
from .sheet import eval_sheet_html

REPEAT = "repeat"                       # 두 번째 요청의 파일 접미사 (input-repeat.png, viewer-repeat.png, ...)


@dataclass
class Fixture:
    id: str
    seed: int
    variant: str
    spec: dict = field(default_factory=dict)
    png: bytes = b""
    gt: dict = field(default_factory=dict)
    repeat_png: bytes = b""             # 두 번째 요청 이미지 (STABILITY_VARIANTS만)

    @property
    def repeated(self) -> bool:
        return self.variant in STABILITY_VARIANTS


@dataclass
class Request:
    """앱에 보내는 요청 하나. 장면마다 첫 요청이 있고, 안정성을 재는 장면은 두 번째 요청이 더 있다."""
    fx: Fixture
    png: bytes
    suffix: str = ""                    # 파일 접미사. 첫 요청은 빈 문자열

    @property
    def name(self) -> str:
        return self.fx.id if not self.suffix else f"{self.fx.id} (두 번째 요청)"

    def file(self, stem: str, ext: str) -> str:
        return f"{stem}-{self.suffix}.{ext}" if self.suffix else f"{stem}.{ext}"


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


def repeat_seed(fx: Fixture) -> int:
    """두 번째 요청 이미지의 노이즈 시드. 같은 장면은 항상 같은 바이트가 된다 (후보와 main이 같은 입력을 받는다)."""
    return (fx.seed * 2654435761 + 0x5F3759DF) % 2**32


def _prepare(fixtures: list[Fixture], out_dir: Path, lab: Lab, registry: dict | None) -> list[Request]:
    """입력 이미지를 준비하고 보낼 요청 목록을 만든다. 첫 요청이 모두 앞에, 두 번째 요청이 뒤에 온다."""
    for fx in fixtures:
        if not fx.png:
            lab.render(fx)
        if fx.repeated and not fx.repeat_png:
            fx.repeat_png = lab.repeat_image(fx.png, repeat_seed(fx))
        d = out_dir / fx.id
        d.mkdir(parents=True, exist_ok=True)
        (d / "input.png").write_bytes(fx.png)
        (d / "gt.json").write_text(json.dumps(fx.spec, indent=2))
    requests = ([Request(fx, fx.png) for fx in fixtures]
                + [Request(fx, fx.repeat_png, REPEAT) for fx in fixtures if fx.repeated])
    for rq in requests:
        if rq.suffix:
            (out_dir / rq.fx.id / rq.file("input", "png")).write_bytes(rq.png)
        if registry is not None:
            registry[hashlib.sha256(rq.png).hexdigest()] = rq.fx.spec
    return requests


def _submit(app: AppHandle, requests: list[Request], smoke: Violations) -> dict:
    submitted = {}
    for rq in requests:
        code, body = http("POST", f"{app.base_url}/api/requests", rq.png, "image/png")
        key = _json_field(code, body, 202, "key")
        if not key:
            smoke.add("post", f"{rq.name}: {code} {body[:120]!r}")
        submitted[rq.name] = {"key": key, "t0": time.time(), "status": "pending" if key else "failed"}
    return submitted


def _wait(app: AppHandle, submitted: dict, smoke: Violations, interval: float, seen: set[str]) -> None:
    deadline = time.time() + REQUEST_TIMEOUT_S
    while time.time() < deadline and any(s["status"] == "pending" for s in submitted.values()):
        if app.pid:  # 앱이 띄운 하위 프로세스를 기록한다 (런타임 요구사항 확인용)
            seen |= process_group_commands(app.pid)
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


def _measure(app: AppHandle, rq: Request, s: dict, out_dir: Path, lab: Lab, smoke: Violations) -> dict:
    """done이 된 요청 하나의 뷰어를 측정해 채점한다. 두 번째 요청도 같은 방법으로 채점한다."""
    fx, d = rq.fx, out_dir / rq.fx.id
    r = {"fixture": fx.id, "seed": fx.seed, "variant": fx.variant, "key": s["key"], "status": s["status"],
         "seconds": s.get("seconds"), "error": s.get("error"), "console_errors": 0,
         "metrics": dict(WORST_METRICS), "fidelity": 0.0, "fov": None}
    if s["status"] == "done":
        scene_file = app.data_dir / "scenes" / f"{s['key']}.js"
        if not scene_file.exists():
            smoke.add("scene_file", f"{rq.name}: {scene_file.name} 없음")
        elif "build" not in scene_file.read_text(errors="replace"):
            smoke.add("scene_file", f"{rq.name}: build 함수가 보이지 않는다")
        before = len(lab.external_requests)
        pred, n_err, err = lab.measure_viewer(f"{app.base_url}/view?key={s['key']}", d / rq.file("viewer", "png"))
        if len(lab.external_requests) > before:
            smoke.add("local_only", f"{rq.name}: {lab.external_requests[before]}")
        r["console_errors"] = n_err
        if pred is None:
            smoke.add("viewer", f"{rq.name}: {err}")
            r["error"] = err
        elif not pred["camera"]["isPerspective"]:
            smoke.add("viewer", f"{rq.name}: camera가 PerspectiveCamera가 아니다")
        elif not pred["roles"].get("ground"):
            smoke.add("ground_mesh", f"{rq.name}: role=ground 메시가 없다")
        else:
            r["metrics"] = compare(fx.gt, pred, fx.spec)
            r["fidelity"] = score_metrics(r["metrics"])
            r["fov"] = round(pred["camera"]["fov"], 4)     # 추정 수직 화각 (안정성 지표에 쓴다)
            (d / rq.file("measured", "json")).write_text(
                json.dumps({k: v for k, v in pred.items() if k != "mask"}, indent=2))
    r["metrics"]["viewer.console_errors"] = r["console_errors"]
    if s.get("seconds") is not None:
        r["metrics"]["run.seconds"] = s["seconds"]
    return r


def stability(first: dict, second: dict) -> dict:
    """같은 장면을 두 번 요청한 결과의 차이. 채점하지 못한 결과는 충실도 0점, 화각 차는 최악값으로 센다."""
    fov = (abs(first["fov"] - second["fov"]) if first["fov"] is not None and second["fov"] is not None
           else STABILITY_WORST_FOV_DELTA)
    return {"stability.fidelity_delta": round(abs(first["fidelity"] - second["fidelity"]), 2),
            "stability.fov_delta_deg": round(fov, 4)}


def _measure_fixture(app: AppHandle, requests: list[Request], submitted: dict, out_dir: Path, lab: Lab,
                     smoke: Violations) -> dict:
    """장면 하나의 요청(첫 요청, 있으면 두 번째 요청)을 채점한다. 행의 점수와 지표는 첫 요청의 것이다."""
    first, *rest = [_measure(app, rq, submitted[rq.name], out_dir, lab, smoke) for rq in requests]
    for second in rest:
        first["metrics"].update(stability(first, second))
        first["repeat"] = {k: second[k] for k in ("key", "status", "seconds", "error", "console_errors", "fidelity",
                                                   "fov")}
        (out_dir / first["fixture"] / f"result-{REPEAT}.json").write_text(
            json.dumps(second, indent=2, ensure_ascii=False))
    (out_dir / first["fixture"] / "result.json").write_text(json.dumps(first, indent=2, ensure_ascii=False))
    return first


def run_eval(app: AppHandle, fixtures: list[Fixture], out_dir: Path, lab: Lab, registry: dict | None = None) -> dict:
    """fixtures를 app에 요청하고 측정·채점한다. 결과는 out_dir/summary.json과 sheet.png에도 남긴다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    smoke = Violations()
    requests = _prepare(fixtures, out_dir, lab, registry)
    seen: set[str] = set()
    if app.pid:
        seen |= process_group_commands(app.pid)
    submitted = _submit(app, requests, smoke)
    _wait(app, submitted, smoke, interval=0.5 if registry is not None else 2, seen=seen)
    results = [_measure_fixture(app, [rq for rq in requests if rq.fx is fx], submitted, out_dir, lab, smoke)
               for fx in fixtures]
    if not any(r["status"] == "done" for r in results):
        smoke.add("done", "done이 된 요청이 하나도 없다")
    summary = summarize(results)
    summary["smoke"] = {"passed": not smoke, "violations": dict(smoke)}
    if app.source_dir:
        summary["code"] = measure_code(app.source_dir)
    if app.pid:
        summary["processes"] = sorted(seen)
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
    print(f"  {'fixture':<16}{'status':<9}{'fidelity':>9}{'fov°':>8}{'pitch°':>8}{'f1':>6}{'pos':>7}{'iou':>6}"
          f"{'Δfid':>7}{'Δfov°':>7}")
    for r in summary["fixtures"]:
        m = r["metrics"]
        stab = (f"{m['stability.fidelity_delta']:>7.1f}{m['stability.fov_delta_deg']:>7.2f}"
                if "stability.fidelity_delta" in m else f"{'-':>7}{'-':>7}")
        print(f"  {r['fixture']:<16}{r['status']:<9}{r['fidelity']:>9.1f}{m['camera.fov_error_deg']:>8.1f}"
              f"{m['camera.pitch_error_deg']:>8.1f}{m['objects.f1']:>6.2f}{m['objects.position_error']:>7.3f}"
              f"{m['reprojection.iou']:>6.2f}{stab}")
    for v in ("public", "holdout"):
        if v in summary:
            m = summary[v]["metrics"]
            stab = (f" · 안정성 충실도 차 {m['stability.fidelity_delta']:.2f}, 화각 차 {m['stability.fov_delta_deg']:.2f}°"
                    if m.get("stability.fidelity_delta") is not None else "")
            print(f"  평균 {v:<8} fidelity {summary[v]['fidelity']:.1f}{stab}")
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


