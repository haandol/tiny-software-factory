# tiny-software-factory

소프트웨어 팩토리의 원칙을 가장 작은 형태로 직접 실행해 본 실험이다.

사람은 Linear에 요구사항 이슈를 넣는다. `factory.py`는 이슈를 가져가 Claude Code에게 예제 제품을 만들게 하고, 정답을 아는 합성 3D 장면으로 결과를 평가한다. eval을 통과한 변경만 제품에 병합하고, 통과하지 못하면 Linear에서 사람에게 넘긴다. 대기열이 비면 발견 루프가 배포된 제품을 평가해, 최종 목표에 못 미친 지표로 다음 이슈를 스스로 만든다.

예제 제품은 사진 한 장을 받아 같은 구도의 three.js 프리비즈 3D 장면을 만드는 Python 서비스다. 제품 안에서는 Codex를 `codex app-server`로 불러 이미지를 측정하고, 측정값을 기하 계산으로 3D 장면으로 바꾼다. 제품 코드는 사람이 한 줄도 쓰지 않았다. 빈 저장소에서 시작해 모든 커밋을 팩토리가 만들었다.

## 어디서부터 읽나

| 자료 | 내용 |
|---|---|
| [docs/guide/00-overview.md](docs/guide/00-overview.md) | 전체 구조와 단계별 소개 자료의 시작. 단계마다 실제 실행 기록과 스크린샷이 있다 |
| [reports/execution-plan/report.md](reports/execution-plan/report.md) | 구현 전에 세운 실행 계획 |
| [reports/symphony-compare/report.md](reports/symphony-compare/report.md) | OpenAI Symphony 명세와 이 팩토리의 비교 |
| [reports/factory-plan/report.md](reports/factory-plan/report.md) | 처음 세운 아키텍처 계획 (이후 여러 번 바뀌었다) |
| [CONTRACT.md](CONTRACT.md) | 평가기가 제품을 관찰하는 계약 |
| `product` 브랜치 | 팩토리가 만든 제품 저장소의 이력. 모든 커밋의 작성자가 `tiny-software-factory`다 |

각 리포트 폴더에는 같은 내용의 독립 HTML(`report.html`)도 있다. 내려받아 브라우저로 열면 다이어그램과 이해 확인 문제가 함께 보인다. 단계별 소개 자료를 HTML 한 장으로 보려면 `uv run docs/build_guide.py`를 실행한다.

## 단계

| 단계 | 내용 | 태그 | 소개 자료 |
|---|---|---|---|
| 0. 준비 | 환경 점검, Linear 준비, 비밀값 격리 | `step-0-foundation` | [00](docs/guide/00-overview.md) |
| 1. 평가 먼저 | 정답 장면 생성기, 채점기, 기준 앱으로 채점기 검증 | `step-1-eval` | [01](docs/guide/01-eval-first.md) |
| 2. 단일 실행기 | 작업 명세 하나를 구현 → 평가 → 재시도 → 병합 → 배포 | `step-2-worker` | [02](docs/guide/02-autonomous-worker.md) |
| 3. 모듈화와 리팩터링 | 팩토리 코드 모듈화, 병합 전 리팩터링 단계, Codex app-server 전환 | `step-3-refactor` | [03](docs/guide/03-refactor.md) |
| 4. Linear 연결 | 이슈 트래커를 제어판으로, Needs Human, `factory-eval` 경로 | `step-4-pipeline` | [04](docs/guide/04-pipeline.md) |
| 5. 발견 루프 | 목표 미달 지표 → 다음 이슈, 측정 잡음 반영 | `step-5-discovery` | [05](docs/guide/05-discovery.md) |
| 6. 학습 루프 | 실행 기록으로 하네스 개선 | 예정 | |
| 7. 정리 | 현재 프로젝트에 적용하는 가이드 | 예정 | |

## 구조

```text
factory.py            진입점 (명령 → tsf/ 모듈), 의존성은 PEP 723 인라인 선언
tsf/                  팩토리 구현
  oracle/             평가 자산: 정답 장면, 채점, 코드 지표, 안정성 지표, 수용 조건 (factory-eval 이슈로만 바뀐다)
  worker.py           생산 루프: worktree, Claude Code, 재시도
  refactor.py         병합 전 리팩터링 단계
  pipeline.py         Linear 상주 실행 (up)
  spec.py             이슈 → 작업 명세
  eval_change.py      factory-eval 이슈 처리
  discovery.py        발견 루프
harness/v1/           에이전트 지시문
docs/guide/           단계별 소개 자료와 스크린샷
docs/capture.py       소개 자료용 캡처 도구
reports/              계획·비교 리포트 (md, json, html)
work/                 로컬 작업 명세 예시
```

## 직접 실행하기

필요한 것: Python 3.13, uv, git, Claude Code, Codex CLI, AWS CLI(Bedrock을 쓰는 default profile), Linear 팀과 개인 API 키.

```sh
echo 'LINEAR_API_KEY=lin_api_...' > .env
uv run factory.py init           # 로컬 준비 (제품 전용 Codex 설정, three.js, 빈 제품 저장소, Chromium)
uv run factory.py linear-setup   # Linear 라벨·Needs Human 상태 준비 (팀 키는 FACTORY_LINEAR_TEAM, 기본 SWF)
uv run factory.py doctor         # 환경 점검 (Claude Code·Codex를 한 번씩 호출)
uv run factory.py calibrate      # 채점기 검증 (기준 앱만 쓰므로 모델 호출 없음)
uv run factory.py run work/LOCAL-1.yaml   # 작업 명세 하나를 구현·평가·(리팩터링)·병합·배포 (http://127.0.0.1:8000)
uv run factory.py up                      # Linear를 폴링하며 이슈를 처리하고, 비면 발견 루프가 다음 이슈를 만든다
uv run factory.py metrics product         # 코드 지표 (파일·함수 길이, 분기 복잡도)
```

`product/`는 팩토리가 만드는 별도 git 저장소라 이 브랜치에는 없다. `init`이 빈 저장소를 만들고, 이 저장소의 `product` 브랜치에 실험에서 만들어진 이력이 있다.

## 이 실험의 전제

- 팩토리 코드(`factory.py`, `tsf/`, `docs/`)는 Claude Code 세션에서 사람과 함께 작성했다. 제품 코드는 팩토리만 작성했다.
- 리뷰 게이트는 없다. 대신 eval을 통과한 변경만 병합하고, 평가 기준은 사람이 등록한 `factory-eval` 이슈로만 바뀐다.
- 에이전트는 권한 확인 없이 같은 머신에서 실행된다. 격리(Docker 등)는 하지 않았다. 신뢰할 수 있는 환경에서만 실행한다.
