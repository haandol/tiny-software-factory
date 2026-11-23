"""평가 정책: 평가 세트, 최종 목표, 회귀 허용폭, 코드 지표 기준.

이 값은 평가 자산이다. 제품 작업도 개선 루프도 바꿀 수 없고, 사람이 등록한
factory-eval 이슈로만 바뀐다.
"""

IMAGE_W, IMAGE_H = 640, 480
PUBLIC_SEEDS = [1101, 2202, 3303, 4404]
HOLDOUT_COUNT = 4
REQUEST_TIMEOUT_S = 900                 # 요청 하나가 done이 될 때까지 기다리는 최대 시간
VARIANT_OPTS = {                        # 렌더링 변형. 홀드아웃만 그림자와 노이즈를 섞는다
    "public": {},
    "holdout": {"shadows": True, "textureNoise": True, "imageNoise": 18},
}
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
# 회귀 허용폭. 실행 계획의 규칙대로 "2점과 실측 흔들림 중 큰 값"으로 정했다.
# 2단계 실측(2026-11-23): 같은 main·같은 시드에서 홀드아웃 평균 77.1 / 70.8 / 69.8 (최대 7.3점 차),
# 공개 세트 평균 97.4 / 97.0 / 97.2 (최대 0.4점 차). 가림 결함을 고치면 다시 잰다.
REGRESSION_TOLERANCE = 8.0              # 홀드아웃: 후보가 같은 시드의 main보다 이만큼 넘게 낮으면 회귀
PUBLIC_TOLERANCE = 2.0                  # 공개 세트: 리팩터링 전보다 이만큼 넘게 낮으면 거부

# 장면 충실도의 최종 목표. 이슈 하나의 성공 조건은 이 중 일부를 한 단계씩 끌어올린다.
FINAL_TARGETS = {
    "camera.fov_error_deg": ("<=", 5.0),
    "camera.pitch_error_deg": ("<=", 3.0),
    "camera.roll_error_deg": ("<=", 3.0),
    "objects.f1": (">=", 0.8),
    "objects.position_error": ("<=", 0.10),
    "objects.size_error": ("<=", 0.20),
    "objects.color_delta_e": ("<=", 15.0),
    "reprojection.iou": (">=", 0.7),
    "viewer.console_errors": ("<=", 0),
}

# 코드 지표 기준. 넘으면 생산 루프가 병합 전에 리팩터링 단계를 실행한다.
CODE_TARGETS = {
    "code.max_file_lines": 400,          # 파일 하나의 코드 줄 수 (빈 줄·주석 제외)
    "code.max_function_lines": 60,       # 함수 하나의 줄 수
    "code.max_complexity": 15,           # 함수 하나의 분기 수 + 1
}

# 장면을 끝내 받지 못했을 때 쓰는 최악값
WORST_METRICS = {
    "camera.fov_error_deg": 90.0, "camera.pitch_error_deg": 90.0, "camera.roll_error_deg": 90.0,
    "objects.f1": 0.0, "objects.position_error": 1.0, "objects.size_error": 1.0,
    "objects.color_delta_e": 100.0, "reprojection.iou": 0.0,
}
