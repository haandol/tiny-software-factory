"""평가 (Evaluation Plane, Oracle).

정답을 아는 3D 장면을 렌더링해 제품에 넣고, 제품 뷰어가 실제로 그린 장면 그래프를 정답과 비교한다.
이 패키지와 CONTRACT.md는 평가 자산이다. 제품 작업도 개선 루프도 고칠 수 없고,
사람이 등록한 factory-eval 이슈로만 바뀐다. 바뀐 뒤에는 calibrate를 통과해야 수용된다.

  policy       평가 세트, 최종 목표, 회귀 허용폭, 측정 잡음, 코드 지표 기준
  scenes       시드 → 정답 장면
  lab          헤드리스 Chromium 실험실 (js/의 브라우저 코드)
  scoring      측정값 비교와 충실도 점수
  code         코드 지표 (파일·함수 길이, 분기 복잡도)
  apps         평가 대상 앱 실행, 기준 앱
  runner       평가 실행과 요약 (홀드아웃은 두 번 요청해 안정성을 잰다)
  sheet        평가 시트 (입력·뷰어·지표 한 장)
  acceptance   작업 명세의 수용 조건 판정
  calibrate    채점기 검증 (정답·빈 장면·오차 주입·흔들림 기준 앱)
"""

from .acceptance import judge, meets, parse_threshold
from .apps import AppHandle, http, start_product_app
from .calibrate import calibrate
from .code import measure_code
from .lab import Lab
from .policy import CODE_TARGETS, FINAL_TARGETS, PUBLIC_TOLERANCE, REGRESSION_TOLERANCE
from .runner import Fixture, eval_app, make_fixtures, new_eval_dir, print_summary, run_eval
from .sheet import eval_sheet_html

__all__ = ["judge", "meets", "parse_threshold", "AppHandle", "http", "start_product_app", "calibrate",
           "measure_code", "Lab", "CODE_TARGETS", "FINAL_TARGETS", "PUBLIC_TOLERANCE", "REGRESSION_TOLERANCE", "Fixture",
           "eval_app", "make_fixtures", "new_eval_dir", "print_summary", "run_eval", "eval_sheet_html"]
