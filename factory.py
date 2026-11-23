#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["playwright==1.65.2", "pyyaml>=6"]
# ///
"""tiny-software-factory — 가장 작은 소프트웨어 팩토리.

이 파일은 진입점이다. 명령을 받아 tsf/의 모듈로 넘긴다. 실행: uv run factory.py <명령>

  단계  명령                         하는 일                                  모듈
  0     init, doctor, linear-setup   로컬 준비, 환경 점검, Linear 준비         tsf/setup.py, tsf/linear.py
  1     eval, calibrate              정답 장면 평가와 채점기 검증              tsf/oracle/
  2     run                          작업 명세 하나를 구현·평가·병합           tsf/worker.py, tsf/delivery.py
  3     metrics, refactor            코드 지표, 리팩터링 단계 (run에도 포함)   tsf/oracle/code.py, tsf/refactor.py
  4     up                           Linear를 제어판으로 상주 실행             tsf/pipeline.py, tsf/spec.py, tsf/eval_change.py
  5     discover (+ up)              발견 루프: 목표 미달 지표 → 다음 이슈      tsf/discovery.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tsf.config import PRODUCT
from tsf.env import load_dotenv


def cmd_init(args):
    from tsf.setup import init
    init()


def cmd_doctor(args):
    from tsf.setup import doctor
    return 0 if doctor(args.skip_agents) else 1


def cmd_linear_setup(args):
    from tsf.linear import setup_team
    setup_team(args.team)


def cmd_eval(args):
    from tsf.oracle import eval_app
    app_dir = Path(args.app).resolve()
    if not (app_dir / "app.py").exists():
        print(f"{app_dir}/app.py가 없다")
        return 1
    seeds = [int(s) for s in args.holdout_seeds.split(",")] if args.holdout_seeds else None
    summary = eval_app(app_dir, args.suite, args.label or app_dir.name, seeds)
    return 0 if summary["smoke"]["passed"] else 1


def cmd_calibrate(args):
    from tsf.oracle import calibrate
    return 0 if calibrate() else 1


def cmd_run(args):
    from tsf.oracle import Lab
    from tsf.worker import load_work_item, run_work_item
    lab = Lab()
    try:
        record = run_work_item(load_work_item(Path(args.work_item)), lab)
    finally:
        lab.close()
    return 0 if record["status"] == "done" else 1


def cmd_refactor(args):
    from tsf.oracle import Lab
    from tsf.refactor import refactor_main
    lab = Lab()
    try:
        record = refactor_main(lab)
    finally:
        lab.close()
    return 0 if record["status"] in ("done", "skipped") else 1


def cmd_up(args):
    from tsf.pipeline import up
    up(once=args.once, interval=args.interval)


def cmd_discover(args):
    from tsf.discovery import discover
    from tsf.oracle import Lab
    lab = Lab()
    try:
        created = discover(lab, force=args.force)
    finally:
        lab.close()
    print(f"만든 이슈: {', '.join(c['identifier'] for c in created) or '없음'}")


def cmd_metrics(args):
    from tsf.oracle import CODE_TARGETS, measure_code
    m = measure_code(Path(args.dir).resolve())
    if args.json:
        print(json.dumps(m, indent=2, ensure_ascii=False))
        return 0
    print(f"파일 {m['code.files']}개, 코드 {m['code.total_lines']}줄, 60줄 넘는 함수 {m['code.long_functions']}개")
    for key, limit in CODE_TARGETS.items():
        mark = "OK  " if m[key] <= limit else "초과"
        where = {"code.max_file_lines": "code.max_file", "code.max_function_lines": "code.max_function",
                 "code.max_complexity": "code.max_complexity_function"}[key]
        print(f"  {mark}  {key:<24} {m[key]:>5} (기준 {limit})  {m[where]}")
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="factory.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="로컬 준비: codex-home, three.js 사본, 제품 저장소, Chromium")
    p = sub.add_parser("doctor", help="환경 점검 (Claude Code·Codex를 한 번씩 호출한다)")
    p.add_argument("--skip-agents", action="store_true", help="모델 호출 점검을 건너뛴다")
    p = sub.add_parser("linear-setup", help="Linear 라벨·Needs Human 상태 준비와 읽기·쓰기 시험")
    p.add_argument("--team", help="Linear 팀 키 (기본 FACTORY_LINEAR_TEAM 또는 SWF)")
    p = sub.add_parser("eval", help="폴더의 app.py를 띄워 정답 장면으로 평가한다")
    p.add_argument("--app", default=str(PRODUCT), help="app.py가 있는 폴더 (기본 product/)")
    p.add_argument("--suite", choices=["public", "holdout", "all"], default="all")
    p.add_argument("--label", help="결과 폴더 이름에 붙일 라벨")
    p.add_argument("--holdout-seeds", help="홀드아웃 시드를 고정한다 (쉼표 구분, 점수 흔들림 측정용)")
    sub.add_parser("calibrate", help="기준 앱(정답·빈 장면·오차 주입)으로 채점기를 검증한다")
    p = sub.add_parser("run", help="작업 명세 하나를 구현·평가·리팩터링·병합까지 실행한다")
    p.add_argument("work_item", help="작업 명세 YAML 경로 (예: work/LOCAL-1.yaml)")
    sub.add_parser("refactor", help="이미 병합된 main에 리팩터링 단계만 실행한다 (동작 보존이 수용 조건)")
    p = sub.add_parser("up", help="Linear를 폴링하며 factory·factory-eval 이슈를 처리한다 (상주)")
    p.add_argument("--once", action="store_true", help="대기열이 비면 끝낸다")
    p.add_argument("--interval", type=int, default=30, help="폴링 간격(초)")
    p = sub.add_parser("discover", help="발견 루프 한 번: main을 평가해 최종 목표 미달 지표를 Linear 이슈로 만든다")
    p.add_argument("--force", action="store_true", help="같은 main 커밋이어도 다시 평가한다")
    p = sub.add_parser("metrics", help="폴더의 Python 코드 지표 (파일·함수 길이, 분기 복잡도)")
    p.add_argument("dir", nargs="?", default=str(PRODUCT))
    p.add_argument("--json", action="store_true")

    args = parser.parse_args()
    handlers = {"init": cmd_init, "doctor": cmd_doctor, "linear-setup": cmd_linear_setup, "eval": cmd_eval,
                "calibrate": cmd_calibrate, "run": cmd_run, "refactor": cmd_refactor, "up": cmd_up, "discover": cmd_discover,
                "metrics": cmd_metrics}
    return handlers[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())
