# tiny-software-factory

소프트웨어 팩토리의 원칙을 가장 작은 형태로 직접 실행해 보는 실험이다.
Linear에 요구사항을 넣으면 `factory.py`가 Claude Code로 예제 제품(`product/app.py`)을 만들고, 정답을 아는 합성 장면으로 평가해 eval을 통과한 변경만 받아들인다.

- 단계별 소개 자료: [docs/guide/00-overview.md](docs/guide/00-overview.md)
- 앱 계약: [CONTRACT.md](CONTRACT.md)
- 실행 계획: [reports/execution-plan/report.md](reports/execution-plan/report.md)

## 빠른 시작

```sh
echo 'LINEAR_API_KEY=lin_api_...' > .env
uv run factory.py init           # 로컬 준비 (codex-home, three.js, 제품 저장소, Chromium)
uv run factory.py linear-setup   # Linear 라벨·Needs Human 상태 준비
uv run factory.py doctor         # 환경 점검 (Claude Code·Codex를 한 번씩 호출)
uv run factory.py calibrate      # 채점기 검증 (기준 앱 세 종류, 모델 호출 없음)
uv run factory.py run work/LOCAL-1.yaml   # 작업 명세 하나를 구현·평가·병합·배포 (http://127.0.0.1:8000)
```

필요한 도구: Python 3.13, uv, git, Claude Code, Codex, AWS CLI(Bedrock default profile).

## 진행 상황

| 단계 | 상태 | 태그 |
|---|---|---|
| 0. 준비 | 완료 | `step-0-foundation` |
| 1. 평가 먼저 | 완료 | `step-1-eval` |
| 2. 단일 실행기 | 완료 | `step-2-worker` |
| 3. 모듈화와 리팩터링 단계 | 진행 중 | `step-3-refactor` |
| 4. Linear 연결 | 예정 | `step-4-pipeline` |
| 5. 발견 루프 | 예정 | `step-5-discovery` |
| 6. 학습 루프 | 예정 | `step-6-learning` |
| 7. 정리 | 예정 | `step-7-demo` |
