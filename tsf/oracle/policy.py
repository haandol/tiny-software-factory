"""평가 정책: 평가 세트, 최종 목표, 회귀 허용폭, 측정 잡음, 코드 지표 기준.

이 값은 평가 자산이다. 제품 작업도 개선 루프도 바꿀 수 없고, 사람이 등록한
factory-eval 이슈로만 바뀐다.
"""

IMAGE_W, IMAGE_H = 640, 480
PUBLIC_SEEDS = [1101, 2202, 3303, 4404]
# SWF-11에서 4 → 8. 홀드아웃 장면은 모두 두 번 요청하므로 안정성 지표도 8장면 × 2요청으로 잰다.
# 4장면 × 2요청일 때는 같은 main에서도 충실도 차가 0.73, 1.15, 2.48, 1.12로 흔들려 목표를 잡을 수 없었다.
HOLDOUT_COUNT = 8
REQUEST_TIMEOUT_S = 900                 # 요청 하나가 done이 될 때까지 기다리는 최대 시간
VARIANT_OPTS = {                        # 렌더링 변형. 홀드아웃만 그림자와 노이즈를 섞는다
    "public": {},
    "holdout": {"shadows": True, "textureNoise": True, "imageNoise": 18},
}
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
# 안정성 (SWF-5). 이 변형의 장면은 앱에 두 번 요청한다 (홀드아웃 HOLDOUT_COUNT장면 × 2요청).
# 두 번째 요청 이미지는 첫 이미지에 채널마다 ±STABILITY_PIXEL_NOISE 이내의 정수 노이즈를 더해 다시 인코딩한
# PNG다. 사람 눈에는 같고 바이트는 다르다.
# 충실도와 다른 지표, 회귀 비교는 첫 요청의 결과로만 계산한다.
STABILITY_VARIANTS = ("holdout",)
STABILITY_PIXEL_NOISE = 2
STABILITY_WORST_FOV_DELTA = 90.0        # 두 결과 중 하나라도 화각을 잴 수 없을 때 쓰는 화각 차
# 회귀 허용폭. 실행 계획의 규칙대로 "2점과 실측 흔들림 중 큰 값"으로 정했다.
# 실측(2026-11-23, SWF-3 병합 후 main, 같은 시드 179425·342669·444967·938929로 세 번 평가):
# 홀드아웃 평균 96.8 / 96.3 / 96.8 (최대 0.5점 차), 공개 세트 평균 97.5 / 97.3 / 97.7 (최대 0.4점 차).
# 두 세트 모두 흔들림이 2점보다 작으므로 허용폭은 2점이다.
REGRESSION_TOLERANCE = 2.0              # 홀드아웃: 후보가 같은 시드의 main보다 이만큼 넘게 낮으면 회귀
PUBLIC_TOLERANCE = 2.0                  # 공개 세트: 리팩터링 전보다 이만큼 넘게 낮으면 거부

# 지표별 측정 잡음 (SWF-11). 발견 루프는 목표와의 차이가 이 값보다 작으면 이슈를 만들지 않고, 한 단계의 폭도
# 이 값보다 작게 잡지 않는다. 잡음보다 작은 차이를 목표로 잡으면 개선이 아니라 운을 쫓게 된다 (SWF-10의 교훈).
# 키는 지표 키(홀드아웃 평균)이고, 홀드아웃 충실도 평균만 "holdout.fidelity"로 둔다.
# 측정: 2026-11-24, main 33a3085, 홀드아웃 8장면 × 2요청, 고정 시드 765030·712061·950298·912989·566351·920342·
# 551771·116978. 같은 평가를 세 번 실행했고, 값은 세 실행의 홀드아웃 평균의 범위(최댓값 − 최솟값)다.
# 예: 홀드아웃 충실도 95.86 / 95.66 / 96.45, 충실도 차 0.871 / 1.221 / 1.850, 화각 차 0.606 / 0.911 / 1.355.
# 주의: 고정 시드에서 잰 실행 간 범위라서 앱의 비결정성만 담는다. 시드가 바뀌면 (발견 루프와 생산 루프는 평가마다
# 홀드아웃 시드를 새로 뽑는다) 장면 구성에 따른 흔들림이 더해져 잡음이 이보다 커질 수 있다.
# 홀드아웃 충실도 잡음 0.79는 회귀 허용폭 2점보다 작으므로 REGRESSION_TOLERANCE는 그대로 둔다.
METRIC_NOISE = {
    "holdout.fidelity": 0.79,
    "camera.fov_error_deg": 0.985,
    "camera.pitch_error_deg": 0.008,
    "camera.roll_error_deg": 0.028,
    "objects.color_delta_e": 0.309,
    "objects.f1": 0.000,
    "objects.position_error": 0.004,
    "objects.size_error": 0.003,
    "reprojection.iou": 0.005,
    "stability.fidelity_delta": 0.979,
    "stability.fov_delta_deg": 0.749,
}

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
    # 안정성 (SWF-5): 같은 장면을 두 번 요청한 결과의 차이. 홀드아웃에서만 잰다
    # SWF-6에서 1.0 → 0.5, 0.5° → 0.2°로 낮췄다. 2026-11-23에 main 8a800b5를 새 홀드아웃 시드 4개로
    # 평가하니 충실도 차 0.77, 화각 차 0.31°(장면별 최대 0.83°)로 SWF-5 목표를 이미 채웠고, 그래서
    # 발견 루프가 개선할 지표를 찾지 못했다. 이 값은 지금 main이 채우지 못하도록 일부러 잡은 다음 목표다.
    # 사용자 지시는 "더 엄격하게"뿐이었고, 0.5와 0.2라는 숫자는 이슈를 쓴 에이전트가 정했다.
    "stability.fidelity_delta": ("<=", 0.5),
    "stability.fov_delta_deg": ("<=", 0.2),
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
