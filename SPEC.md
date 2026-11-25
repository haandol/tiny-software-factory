# tiny-software-factory 명세

상태: 초안 v1 (현재 구현을 기준으로 쓴 명세)

목적: 이슈 트래커에 들어온 요구사항을 코딩 에이전트가 구현하게 하고, 정답을 아는 평가로 판정해 통과한 변경만 제품에 받아들이며, 평가 결과에서 다음 요구사항을 스스로 만드는 서비스를 정의한다.

기준 구현: 이 저장소의 `factory.py`와 `tsf/` (main `290f7ed`, 2026-11-25). 이 문서는 구현과 함께 고친다. 구현과 이 문서가 다르면 그 차이를 부록 B에 적는다.

## 규범 용어

이 문서의 문장 끝은 다음 뜻으로 쓴다. RFC 2119의 키워드와 같다.

| 표현 | 뜻 | RFC 2119 |
|---|---|---|
| ~해야 한다 | 반드시 지킨다 | `MUST`, `REQUIRED` |
| ~해서는 안 된다 | 반드시 하지 않는다 | `MUST NOT` |
| ~하는 것을 권장한다 | 이유가 없으면 지킨다 | `SHOULD`, `RECOMMENDED` |
| ~하지 않는 것을 권장한다 | 이유가 없으면 하지 않는다 | `SHOULD NOT` |
| ~할 수 있다 | 선택이다 | `MAY`, `OPTIONAL` |

`구현 정의`는 동작이 구현 계약에 속하지만 이 명세가 하나의 정책을 정하지 않는다는 뜻이다. 구현은 고른 동작을 문서로 남겨야 한다. 이 문서는 고른 동작을 "현재 구현:"으로 함께 적는다.

## 1. 문제 정의

tiny-software-factory(이하 팩토리)는 이슈 트래커를 계속 읽어 작업을 가져오고, 이슈마다 격리된 작업 공간에서 코딩 에이전트를 실행하고, 결과를 평가해 통과한 변경만 제품에 병합하는 상주 서비스다.

팩토리는 다섯 가지 운영 문제를 푼다.

- 사람이 에이전트를 직접 부르고 결과를 확인하는 일을, 이슈 트래커를 제어판으로 쓰는 반복 가능한 흐름으로 바꾼다.
- 변경을 받아들일지를 사람의 리뷰가 아니라 정답을 아는 평가(Oracle)로 정한다. 평가를 통과하지 못한 변경은 병합하지 않는다.
- 실패한 시도의 증거를 다음 시도에 넘겨, 에이전트가 사람 없이 수렴하게 한다. 정해진 시도 안에 통과하지 못하면 사람에게 넘긴다.
- 평가 기준을 바꾸는 길을 하나로 좁힌다. 평가 기준은 사람이 등록한 평가 변경 이슈로만 바뀐다.
- 대기열이 비면 배포된 제품을 평가해, 최종 목표에 못 미친 지표로 다음 이슈를 만든다.

중요한 경계:

- 팩토리는 스케줄러이자 실행기이고, 트래커에 쓰는 유일한 주체다. 상태 이동, 댓글, 첨부는 팩토리 컨트롤러가 한다. 에이전트는 트래커에 쓰지 않는다.
- 성공한 실행의 끝은 사람의 검토 상태가 아니라 `Done`이다. 사람의 리뷰 게이트는 없다. 대신 eval 관문이 있다.
- 실패한 실행의 끝은 `Needs Human`이다. 사람은 트래커에서 다음 행동을 고른다.
- 팩토리는 신뢰할 수 있는 단일 머신에서 실행되는 것을 전제로 한다. 샌드박스와 승인 정책은 18절에 적는다.

## 2. 목표와 비목표

### 2.1 목표

- 트래커를 일정한 간격으로 폴링해 작업을 하나씩 실행한다.
- 이슈의 자연어를 성공 조건이 있는 작업 명세로 바꾸되, 수용 조건은 프로그램이 정한다.
- 이슈마다 결정적인 작업 공간을 만들고, 실패해도 보존해 이어서 실행할 수 있게 한다.
- 후보 변경을 정답을 아는 합성 장면으로 평가하고, 수용 조건을 모두 통과한 변경만 병합한다.
- 병합 전에 코드 지표를 재고, 기준을 넘으면 동작을 보존하는 리팩터링 단계를 거친다.
- 평가 자산은 별도 경로(`factory-eval`)로만 바꾸고, 바꾼 뒤에는 채점기 검증을 통과해야 한다.
- 대기열이 비면 발견 루프가 최종 목표와의 차이를 측정 잡음을 고려해 다음 이슈로 만든다.
- 비용 상한과 연속 실패로 스스로 멈춘다.
- 영속 데이터베이스 없이 트래커와 파일 시스템으로 재시작을 복구한다.

### 2.2 비목표

- 여러 작업의 동시 실행과 병합 큐.
- 웹 대시보드나 다중 사용자 제어판.
- 사람의 코드 리뷰 게이트.
- 에이전트 실행의 강한 샌드박스. 신뢰할 수 있는 머신을 전제로 한다.
- 범용 워크플로 엔진. 생산·발견 루프의 정책은 코드와 평가 정책 파일에 있다.
- 실행 기록으로 하네스를 고치는 학습 루프. 계획에는 있지만 이 명세의 범위 밖이다 (21.2).

## 3. 시스템 개요

### 3.1 주요 구성요소

1. **트래커 어댑터** (`tsf/linear.py`)
   - Linear GraphQL로 이슈를 읽고, 상태를 옮기고, 댓글과 첨부를 쓰고, 이슈를 만든다.
   - 팀의 라벨과 상태를 준비한다 (`linear-setup`).

2. **오케스트레이터** (`tsf/pipeline.py`, 명령 `up`)
   - 폴링 틱마다 일시 정지 확인, 취소된 작업 공간 정리, 후보 선점, 실행, 결과 보고를 한다.
   - 대기열이 비면 발견 루프를 부른다.
   - 상태 이동은 이 구성요소만 한다.

3. **작업 명세 변환기** (`tsf/spec.py`)
   - 이슈 본문과 사람 댓글을 작업 명세로 바꾼다. LLM은 구조화만 하고, 수용 조건과 목표값 검증은 프로그램이 한다.

4. **생산 실행기** (`tsf/worker.py`, 명령 `run`)
   - 작업 명세 하나를 구현 → 평가 → 재시도 → 리팩터링 → 병합·배포까지 끌고 간다.

5. **리팩터링 단계** (`tsf/refactor.py`, 명령 `refactor`)
   - eval을 통과한 구현의 코드 지표가 기준을 넘으면 리팩터링 에이전트를 한 번 실행하고, 동작·충실도·구조 조건을 모두 통과할 때만 받아들인다.

6. **전달** (`tsf/delivery.py`)
   - 통과한 커밋을 제품 main에 squash 병합하고 로컬 프로덕션 서버를 다시 띄운다. 배포가 실패하면 main을 되돌린다.

7. **평가 자산 변경 경로** (`tsf/eval_change.py`)
   - `factory-eval` 이슈를 팩토리 저장소의 작업 공간에서 실행하고, 변경 범위와 채점기 검증으로 판정한다.

8. **발견 루프** (`tsf/discovery.py`, 명령 `discover`)
   - 배포된 main을 평가해 최종 목표 미달 지표를 단계적 목표의 이슈로 만든다.

9. **평가기 (Oracle)** (`tsf/oracle/`, 명령 `eval`, `calibrate`, `metrics`)
   - 정답 장면 생성, 헤드리스 브라우저 렌더링, 제품 실행과 측정, 채점, 안정성, 계약 스모크, 프로세스 관찰, 코드 지표, 수용 조건 판정, 채점기 검증을 맡는다.
   - 이 패키지와 `CONTRACT.md`가 평가 자산이다.

10. **하네스** (`harness/<버전>/`, `tsf/harness.py`)
    - 에이전트에게 주는 지시문 템플릿이다.

11. **제품 런타임** (`product/`, 별도 git 저장소)
    - 팩토리가 만드는 제품이다. 제품은 `CONTRACT.md`를 지켜야 하고, 장면 생성에 Codex를 `codex app-server`로 부른다.

12. **로컬 기록** (`var/`)
    - 실행 기록, 평가 기록, 작업 공간, 상태 파일, 제품 전용 Codex 설정을 둔다. git에 넣지 않는다.

### 3.2 계층

| 계층 | 맡는 일 | 구현 |
|---|---|---|
| 정책 | 사람이 정한 실행 정책, 평가 정책, 에이전트 지시문 | `tsf/config.py`, `tsf/oracle/policy.py`, `harness/v1/`, `CONTRACT.md` |
| 조정 | 폴링, 선점, 조정, 일시 정지, 발견 루프 호출 | `tsf/pipeline.py` |
| 실행 | 작업 공간, 에이전트 실행, 재시도, 리팩터링, 병합 | `tsf/worker.py`, `tsf/refactor.py`, `tsf/delivery.py`, `tsf/eval_change.py` |
| 평가 | 정답 장면, 측정, 채점, 판정, 채점기 검증 | `tsf/oracle/` |
| 연동 | Linear, Claude Code, Codex, git | `tsf/linear.py`, `tsf/worker.py`(`run_claude`), `tsf/codex.py`, `tsf/env.py` |
| 관측 | 실행 기록, 평가 시트, 트래커 댓글 | `var/runs/`, `var/evals/`, Linear 댓글 |

### 3.3 외부 의존성

| 의존성 | 용도 | 필수 |
|---|---|---|
| Linear API (개인 API 키) | 제어판 | 예 |
| Claude Code CLI (Amazon Bedrock, AWS default profile) | 구현·리팩터링·평가 변경 에이전트, 작업 명세 변환, 발견 관찰 | 예 |
| Codex CLI (`codex app-server`, Bedrock) | 제품의 장면 생성 | 예 (제품 쪽) |
| Python 3.13, uv | 팩토리와 제품 실행 (PEP 723 인라인 의존성) | 예 |
| git | 제품 저장소, 작업 공간(worktree), 병합 | 예 |
| Playwright Chromium (SwiftShader WebGL2) | 정답 렌더링과 뷰어 측정 | 예 |
| three.js 0.187.0 사본 (`vendor/`) | 정답 렌더링, 제품 뷰어 | 예 |
| AWS CLI | default profile 자격 증명과 리전 확인 | 예 |

## 4. 핵심 도메인 모델

### 4.1 엔티티

#### 4.1.1 이슈

트래커 어댑터가 정규화한 이슈 레코드다.

| 필드 | 형식 | 뜻 |
|---|---|---|
| `id` | 문자열 | 트래커 내부 ID. 조회와 쓰기에 쓴다 |
| `identifier` | 문자열 | 사람이 읽는 키 (예 `SWF-13`). 작업 공간과 브랜치 이름의 원본 |
| `title` | 문자열 | |
| `description` | 문자열 | 없으면 빈 문자열 |
| `priority` | 정수 또는 null | 1(긴급)~4(낮음), 0 또는 null은 없음 |
| `created_at` | ISO-8601 문자열 | |
| `url` | 문자열 | |
| `state` | 문자열 | 트래커 상태 이름 |
| `labels` | 문자열 목록 | 소문자로 바꾸고 정렬한다 |
| `comments` | 목록 | `{body, createdAt}`, 오래된 순 |

#### 4.1.2 작업 명세 (Work Item)

에이전트가 실행할 단위다. YAML로 작업 공간의 `.factory/work-item.yaml`에 쓴다. 로컬 실행(`run`)은 같은 형식의 파일을 직접 받는다 (예 `work/LOCAL-1.yaml`).

| 필드 | 필수 | 뜻 |
|---|---|---|
| `work_item` | 예 | 작업 ID. 트래커 이슈면 `identifier` |
| `goal` | 예 | 원하는 결과 한두 문장. 구현 방법은 적지 않는다 |
| `acceptance` | 예 | 수용 조건 목록 (4.1.3) |
| `target` | 트래커 경로에서 예 | `product` 또는 `eval` |
| `keep`, `drop`, `constraints`, `notes` | 아니오 | 이슈에 적힌 것만 옮긴 목록 |
| `human_notes` | 아니오 | 사람 댓글 원문 (에이전트에게만 보이고 트래커 보고에는 뺀다) |
| `source_issue` | 아니오 | 이슈 주소 |
| `_spec_cost_usd` | 아니오 | 변환 비용. 밑줄로 시작하는 키는 보고에 쓰지 않는다 |

#### 4.1.3 수용 조건

`acceptance`의 항목이다. 모두 통과해야 eval 통과다. 판정 규칙은 13.9에 있다.

| 형식 | 뜻 | 경로 |
|---|---|---|
| `run` | 앱이 계약대로 시작된다 | 제품 |
| `contract-smoke` | 평가 중 계약 위반이 없다 | 제품 |
| `{process: <명령>, absent: <명령>}` | 평가 중 앱의 프로세스 그룹에 `process`가 있었고 `absent`는 없었다 | 제품 |
| `{suite: public\|holdout, metrics: {<지표 키>: "<= 값" \| ">= 값"}}` | 해당 세트 평균 지표가 목표를 채운다 | 제품 |
| `holdout-regression` | 후보의 홀드아웃 충실도가 같은 시드의 main보다 허용폭 넘게 낮지 않다 | 제품 |
| `calibrate` | 작업 공간의 코드로 돌린 채점기 검증이 통과한다 | 평가 자산 |
| `eval-scope` | 바뀐 파일이 평가 자산 범위 안에만 있고 하나 이상이다 | 평가 자산 |

#### 4.1.4 작업 공간

| 필드 | 제품 경로 | 평가 자산 경로 |
|---|---|---|
| 저장소 | `product/` | 팩토리 저장소 |
| 경로 | `var/worktrees/<작업 공간 키>` | `var/worktrees-factory/<키>` |
| 브랜치 | `factory/<작업 공간 키>` | `factory-eval/<키>` |
| 기준 | 새로 만들 때 `main`에서 갈라진다 | 같다 |

#### 4.1.5 실행 기록 (Run Record)

실행 하나의 기록이다. `var/runs/<실행 ID>/run.json`에 남는다.

| 필드 | 뜻 |
|---|---|
| `run_id` | `<작업 공간 키>-<YYYYmmdd-HHMMSS>` |
| `work_item`, `branch`, `worktree`, `harness` | 작업 ID, 브랜치, 작업 공간 경로, 하네스 버전 |
| `target` | `product` 또는 `eval` (트래커 경로에서 붙인다) |
| `attempts` | 시도 목록 (4.1.6) |
| `refactor` | 리팩터링 단계 결과 (14.2). 제품 경로만 |
| `delivery` | 병합·배포 결과 `{merged, deployed, previous, url, error}`. 평가 자산 경로는 `{merged, repo: "factory"}` |
| `best_attempt` | 포기했을 때 남긴 최고점 시도 번호 |
| `status` | 7.3의 실행 상태 |
| `started`, `finished` | ISO-8601 |
| `cost_usd` | 시도와 리팩터링 단계의 Claude Code 비용 합 |
| `codex_calls` | 평가에서 앱에 보낸 요청 수의 합 |
| `reload` | 평가 자산 경로가 병합했으면 true |

#### 4.1.6 시도 (Attempt)

| 필드 | 뜻 |
|---|---|
| `attempt` | 1부터 |
| `commit` | 시도 뒤 팩토리가 만든 커밋 (바뀐 것이 없으면 null) |
| `claude` | `{exit, model, cost_usd, turns, tool_calls, seconds, subtype, is_error, summary}` |
| `verdict` | 판정 (4.1.8) |
| `eval_dir` | 평가 기록 폴더 (제품 경로) |
| `score` | `{public, holdout, main_same_seeds}` 충실도 (제품 경로) |
| `code` | 후보의 코드 지표 (제품 경로) |
| `codex_calls`, `generation_seconds` | 앱 요청 수, 요청마다 done까지 걸린 초 (제품 경로) |

#### 4.1.7 평가 요약 (Eval Summary)

평가 한 번의 결과다. 평가 기록 폴더의 `summary.json`에 남는다.

| 필드 | 뜻 |
|---|---|
| `overall`, `public`, `holdout` | `{count, fidelity, metrics}`. `fidelity`는 장면 충실도 평균, `metrics`는 지표별 평균 |
| `fixtures` | 장면별 결과 `{fixture, seed, variant, key, status, seconds, error, console_errors, metrics, fidelity, fov, repeat}` |
| `smoke` | `{passed, violations: {항목: [사례]}}` |
| `code` | 앱 소스 폴더의 코드 지표 (13.8) |
| `processes` | 평가 중 관찰한 앱 프로세스 그룹의 명령줄 목록 |

#### 4.1.8 판정 (Verdict)

| 필드 | 뜻 |
|---|---|
| `passed` | 모든 조건이 통과했는가 |
| `conditions` | `{name, ok, detail}` 목록. 수용 조건마다 하나 이상 |
| `failures` | 지표 조건이 실패했을 때 `{metric, suite, expected, actual, worst_fixtures}` |
| `files` | 평가 자산 경로에서 바뀐 파일 목록 |

#### 4.1.9 장면 (Fixture)

| 필드 | 뜻 |
|---|---|
| `id` | `public-01`~`public-04`, `holdout-<시드>` |
| `seed`, `variant` | 시드, `public` 또는 `holdout` |
| `spec` | 시드로 만든 정답 장면 명세 |
| `png`, `repeat_png` | 입력 이미지, 두 번째 요청 이미지 (홀드아웃만) |
| `gt` | 정답 장면을 측정한 값 |

#### 4.1.10 로컬 상태 파일

| 파일 | 내용 | 쓰는 곳 |
|---|---|---|
| `var/discovery.json` | 마지막 발견 평가의 제품 커밋, 평가 자산 커밋, 미달 지표, 만든 이슈 | 발견 루프 |
| `var/pause.json` | 열린 일시 정지 이슈 `{id, identifier, reason}` | 오케스트레이터 |
| `var/pause-ack.json` | 사람이 일시 정지를 푼 기록 `{budget_date, runs_seen, resolved}` | 오케스트레이터 |
| `var/prod.pid`, `var/prod.log`, `var/prod-data/` | 로컬 프로덕션 서버 | 전달 |
| `var/doctor.json` | 마지막 환경 점검에서 본 모델과 비용 | `doctor` |
| `var/codex-home/config.toml` | 제품 전용 Codex 설정 | `init` |

### 4.2 식별자와 정규화 규칙

- **작업 공간 키** (제품 경로): `identifier`에서 `[A-Za-z0-9._-]` 밖의 문자를 `_`로 바꾼다. 바뀐 문자가 있으면 원래 ID의 SHA-256 앞 16자를 `-`로 붙인다. 서로 다른 ID가 같은 키가 되지 않게 하기 위해서다.
- **평가 자산 작업 공간 키**: 같은 치환을 하되 해시를 붙이지 않는다 (부록 B).
- **라벨**: 앞뒤 공백을 지우고 소문자로 비교한다.
- **팩토리 댓글 표시**: 팩토리가 쓰는 댓글은 `**[factory]**`로 시작해야 한다. 이 표시로 시작하지 않는 댓글은 사람 댓글로 보고 작업 명세의 입력으로 쓴다.
- **실행 ID**: `<작업 공간 키>-<YYYYmmdd-HHMMSS>` (로컬 시각).
- **평가 기록 ID**: `<YYYYmmdd-HHMMSS>-<라벨>`.
- **팩토리 커밋 작성자**: `tiny-software-factory <factory@localhost>`. 팩토리가 만드는 커밋(시도, 병합)은 모두 이 작성자를 써야 한다.

## 5. 저장소 계약

Symphony가 정책을 `WORKFLOW.md` 하나에 두는 것과 달리, 팩토리는 정책을 성격별로 나눠 저장소에 둔다. 나눈 기준은 "누가 바꿀 수 있는가"다.

### 5.1 저장소 구성

```text
factory.py            진입점 (명령 → tsf/ 모듈). PEP 723 인라인 의존성
tsf/                  팩토리 구현
  config.py           경로와 실행 정책
  oracle/             평가 자산: 정답 장면, 측정, 채점, 판정, 채점기 검증, 평가 정책(policy.py)
harness/v1/           에이전트 지시문 템플릿
CONTRACT.md           제품과 평가기 사이의 계약 (평가 자산)
product/              제품 저장소 (별도 git, git에 넣지 않는다)
var/                  실행 중에 생기는 모든 것 (git에 넣지 않는다)
vendor/               three.js 사본 (init이 받는다, git에 넣지 않는다)
work/                 로컬 작업 명세 예시
.env                  LINEAR_API_KEY (git에 넣지 않는다)
```

### 5.2 하네스

- 하네스는 `harness/<버전>/<이름>.md` 파일이다. 현재 버전은 `v1`이다 (`config.HARNESS_VERSION`).
- 템플릿은 Python `str.format`으로 채운다. 값을 주지 않으면 원문 그대로 쓴다.
- 실행 기록은 자신이 쓴 하네스 버전을 남겨야 한다.

| 템플릿 | 쓰는 곳 | 채우는 값 |
|---|---|---|
| `implement-system` | 구현 에이전트의 시스템 지시문 | `self_eval` |
| `implement-task` | 구현 시도 | `work_item_id`, `work_item_yaml`, `attempt`, `max_attempts`, `feedback_hint` |
| `refactor-system` | 리팩터링 에이전트의 시스템 지시문 | `self_eval` |
| `refactor-task` | 리팩터링 단계 | `work_item_id`, `metrics`, `public_before`, `tolerance`, `acceptance` |
| `eval-system` | 평가 자산 에이전트의 시스템 지시문 | 없음 |
| `eval-task` | 평가 자산 시도 | `work_item_id`, `attempt`, `max_attempts`, `work_item_yaml`, `feedback_hint` |
| `spec-task` | 작업 명세 변환 | `identifier`, `title`, `description`, `comments`, `metrics` |

하네스가 에이전트에게 지켜야 할 규칙으로 알리는 것:

- 작업 명세는 `.factory/work-item.yaml`, 이전 실패는 `.factory/feedback.json`에 있다.
- `.factory/` 아래 파일은 읽기만 한다.
- git 커밋은 하지 않는다. 팩토리가 커밋한다.
- 끝내기 전에 `.factory/attempt-notes.md`(리팩터링은 `refactor-notes.md`)에 바꾼 것, 확인한 것, 남은 문제를 적는다.
- 구현 에이전트: 계약을 지키고, Codex는 `codex app-server`로 부르고, 공개 세트의 정답을 코드에 넣지 않는다.
- 리팩터링 에이전트: 동작, 계약, Codex 지시문의 의미를 바꾸지 않는다. 보인 버그는 고치지 않고 노트에 적는다.
- 평가 자산 에이전트: `tsf/oracle/`와 `CONTRACT.md`만 고친다. 채점기 검증의 판정값을 느슨하게 바꾸지 않는다.

### 5.3 앱 계약

제품과 평가기 사이의 계약은 `CONTRACT.md`가 정한다. 요약:

- 실행: `uv run app.py --port <port> --data-dir <dir>`. 평가기는 포트가 열릴 때까지 180초 기다린다.
- HTTP: `POST /api/requests`(이미지 바이트) → `202 {"key"}`, `GET /api/requests/<키>` → `200 {"status": pending|done|failed, "error"}`, `GET /view?key=<키>` → 뷰어 HTML.
- 장면 파일: `<data-dir>/scenes/<키>.js`, `export function build(THREE) { return { scene, camera } }`.
- 뷰어: `window.__PREVIZ__ = { ready, scene, camera }`. 30초 안에 객체, 60초 안에 `ready`.
- 장면 규칙: y축이 위, 메시마다 `userData.role`(`ground` 필수, `wall`, `object`), 길이는 카메라 높이로 정규화해 비교한다.
- 런타임: Codex는 `codex app-server`로 부르고 앱과 같은 프로세스 그룹에서 실행한다. 모델은 `CODEX_HOME` 설정을 따른다.

제품은 계약을 지켜야 한다. 평가기는 계약 밖의 앱 내부를 가정해서는 안 된다.

### 5.4 평가 정책

평가 정책은 `tsf/oracle/policy.py`에 있다. 평가 세트, 최종 목표, 회귀 허용폭, 측정 잡음, 코드 지표 기준, 안정성 설정을 담는다. 값은 6.2에 있다. 정책을 바꾼 이유와 근거(측정 날짜, 커밋, 시드)는 정책 파일의 주석에 남기는 것을 권장한다.

### 5.5 변경 권한

| 대상 | 바꿀 수 있는 주체 | 경로 |
|---|---|---|
| 제품 코드 (`product/`) | 팩토리만 | `factory` 이슈 또는 로컬 작업 명세 → eval 통과 → 병합 |
| 평가 자산 (`tsf/oracle/`, `CONTRACT.md`) | 팩토리만 | 사람이 등록한 `factory-eval` 이슈 → 범위 + 채점기 검증 → 병합 |
| 팩토리 코드 (`factory.py`, `tsf/`의 나머지, `docs/`) | 사람 | 직접 커밋 |
| 실행 정책 (`tsf/config.py`), 하네스 | 사람 | 직접 커밋 |

- 제품 작업, 리팩터링 단계, 발견 루프는 평가 자산을 바꿔서는 안 된다.
- 평가 자산 경로는 평가 자산 범위 밖의 파일을 바꿔서는 안 된다. 바꾸면 수용되지 않는다.
- 사람도 평가 기준을 직접 바꾸지 않는 것을 권장한다. 평가 기준이 틀렸으면 `factory-eval` 이슈를 등록한다.

## 6. 설정

### 6.1 설정 출처

설정은 다음 순서로 정해진다.

1. 코드에 적힌 기본값 (`tsf/config.py`, `tsf/oracle/policy.py`, 각 모듈의 상수)
2. 환경 변수 (아래 표). 실행할 때 `.env`를 읽어 이미 있는 환경 변수는 덮어쓰지 않고 채운다.
3. 명령줄 인자 (`up --interval`, `eval --suite` 등)

### 6.2 설정 값 요약

실행 정책:

| 값 | 기본 | 설정 | 뜻 |
|---|---|---|---|
| Linear 팀 키 | `SWF` | `FACTORY_LINEAR_TEAM` | |
| Linear API 키 | 없음 (필수) | `LINEAR_API_KEY` | 하위 프로세스에 넘기지 않는다 |
| 폴링 간격 | 30초 | `up --interval` | |
| 시도 횟수 | 3 | `MAX_ATTEMPTS` | 작업 하나의 구현 시도 상한 |
| 시도당 Claude Code 비용 상한 | $5 | `CLAUDE_BUDGET_PER_ATTEMPT_USD` | `--max-budget-usd` |
| 시도당 시간 상한 | 3600초 | `CLAUDE_TIMEOUT_S` | |
| 하루 비용 상한 | $400 | `FACTORY_DAILY_BUDGET_USD` | 개발 중 값 |
| 연속 Needs Human 상한 | 3 | `MAX_CONSECUTIVE_NEEDS_HUMAN` | |
| 프로덕션 포트 | 8000 | `FACTORY_PROD_PORT` | |
| 하네스 버전 | `v1` | `HARNESS_VERSION` | |
| 제품 Codex 모델 | `us.openai.gpt-6.1-sol` | `CODEX_MODEL` | 제품 전용 Codex 설정에 쓴다 |
| 발견 루프 한 번에 만드는 이슈 | 2 | `MAX_NEW_PER_RUN` | |
| 열린 자동 이슈 상한 | 3 | `MAX_OPEN_AUTO` | Backlog, Todo, In Progress, Needs Human |

평가 정책 (`tsf/oracle/policy.py`, 평가 자산):

| 값 | 현재 | 뜻 |
|---|---|---|
| `PUBLIC_SEEDS` | 1101, 2202, 3303, 4404 | 공개 세트 시드 |
| `HOLDOUT_COUNT` | 8 | 평가마다 새로 뽑는 홀드아웃 장면 수 |
| `IMAGE_W`, `IMAGE_H` | 640, 480 | 입력 이미지와 뷰어 뷰포트 |
| `VARIANT_OPTS` | 홀드아웃만 그림자, 질감 노이즈, 이미지 노이즈 18 | 렌더링 변형 |
| `REQUEST_TIMEOUT_S` | 900 | 요청이 done이 될 때까지 기다리는 최대 시간 |
| `STABILITY_VARIANTS`, `STABILITY_PIXEL_NOISE` | 홀드아웃, ±2 | 두 번째 요청 |
| `REGRESSION_TOLERANCE` | 2.0 | 홀드아웃 회귀 허용폭 |
| `PUBLIC_TOLERANCE` | 2.0 | 리팩터링 단계의 공개 세트 허용폭 |
| `FINAL_TARGETS` | 13.4 표 | 지표별 최종 목표 |
| `METRIC_NOISE` | 15.2 | 지표별 측정 잡음 |
| `CODE_TARGETS` | 파일 400줄, 함수 60줄, 복잡도 15 | 코드 지표 기준 |

### 6.3 다시 읽기

- 실행 정책과 하네스는 프로세스를 시작할 때 읽는다. 바꾸면 `up`을 다시 시작해야 한다.
- 평가 자산 경로가 팩토리 main에 병합하면, 오케스트레이터는 자신을 같은 인자로 다시 실행해야 한다 (`os.execv`). 새 평가 코드로 다음 작업을 판정하기 위해서다.

### 6.4 실행 전 점검 (`doctor`)

`doctor`는 루프가 도중에 실패할 원인을 시작 전에 확인한다. 하나라도 실패하면 0이 아닌 코드로 끝나야 한다.

| 점검 | 통과 조건 |
|---|---|
| 도구 | `git`, `uv`, `claude`, `codex`, `aws`가 있다 |
| 로컬 준비 | 제품 전용 Codex 설정, three.js 사본, 제품 저장소가 있다 (`init`) |
| AWS | default profile로 `sts get-caller-identity`가 성공한다 |
| 비밀값 격리 | 하위 프로세스 환경에 `LINEAR_API_KEY`가 없다 |
| Chromium | 헤드리스 Chromium이 WebGL2 컨텍스트를 만든다 |
| Linear | 팀이 있고 라벨 3개와 `Needs Human` 상태가 준비됐다 |
| Claude Code | `--bare` 비대화식 호출이 정해진 답을 돌려준다 (`--skip-agents`로 건너뛸 수 있다) |
| Codex | 제품과 같은 방식(전용 `CODEX_HOME`, `codex app-server`)으로 정해진 답을 돌려준다 (같다) |

## 7. 상태 기계

오케스트레이터만 트래커 상태를 옮긴다. 메모리에 따로 두는 선점 상태는 없다. 선점의 기록은 트래커의 `In Progress`다.

### 7.1 트래커 상태

| 상태 | 뜻 | 팩토리의 동작 |
|---|---|---|
| `Backlog` | 아직 대기열이 아니다 | 읽지 않는다 (발견 루프의 열린 이슈 수에는 센다) |
| `Todo` | 대기열. `factory` 또는 `factory-eval` 라벨이 있으면 후보 | 선점해 실행한다 |
| `In Progress` | 팩토리가 실행 중 | 시도 전마다 이 상태인지 다시 확인한다 |
| `Done` | eval을 통과해 병합됐다 | 아무것도 하지 않는다 |
| `Needs Human` | 정해진 시도 안에 통과하지 못했거나 명세로 바꾸지 못했다 | 아무것도 하지 않는다. 사람이 고른다 |
| `Canceled` | 사람이 취소했다 | 작업 공간을 지운다 (브랜치는 남긴다) |

`Needs Human`은 `linear-setup`이 만드는 사용자 정의 상태이고, 종류는 `started`다.

### 7.2 상태 이동

| 이동 | 주체 | 계기 |
|---|---|---|
| (없음) → `Todo` | 사람, 발견 루프 | 이슈 등록 |
| `Todo` → `In Progress` | 오케스트레이터 | 선점 |
| `In Progress` → `Done` | 오케스트레이터 | 제품: eval 통과 + 병합 + 배포. 평가 자산: 범위 + 채점기 검증 통과 + 병합 |
| `In Progress` → `Needs Human` | 오케스트레이터 | 시도 소진, 작업 명세 변환 실패, 배포 실패 |
| `In Progress` → `Todo` | 오케스트레이터 | 시작할 때 이전 실행이 끊긴 이슈를 되돌린다 |
| `Needs Human` → `Todo` | 사람 | 힌트 댓글을 달고 다시 실행 |
| `Needs Human` → `Canceled` | 사람 | 이슈를 나누거나 다시 쓴다 |
| `In Progress` → (다른 상태) | 사람 | 실행 중 조정 (8.5) |
| 일시 정지 이슈 → `Done` | 사람 | 일시 정지 해제 (8.7) |

### 7.3 실행 상태

실행 기록의 `status`다.

| 상태 | 뜻 | 트래커 |
|---|---|---|
| `running` | 실행 중 | `In Progress` |
| `done` | 병합(과 배포)까지 끝났다 | `Done` |
| `needs_human` | 시도를 다 썼거나 배포에 실패했다 | `Needs Human` |
| `canceled` | 시도 전 확인에서 이슈가 활성 상태가 아니었다 | 그대로 둔다 |
| `skipped`, `rejected` | `refactor` 명령만: 리팩터링이 필요 없었다, 받아들이지 않았다 | (트래커 없음) |

### 7.4 시도 수명주기

제품 경로의 시도 하나는 다음 단계를 거친다.

1. `조정 확인` — 이슈가 아직 `In Progress`인지 본다 (트래커 경로)
2. `지시문 구성` — 하네스와 작업 명세로 지시문을 만든다. 이전 피드백이 있으면 먼저 읽으라고 알린다
3. `에이전트 실행` — Claude Code를 작업 공간에서 한 번 실행한다
4. `커밋` — 바뀐 파일을 팩토리 작성자로 커밋한다
5. `평가` — 후보와 main을 같은 홀드아웃으로 평가한다
6. `판정` — 수용 조건을 하나씩 판정한다
7. `보고` — 트래커에 시도 결과 댓글을 쓴다
8. 통과하면 `전달`(14.1), 실패하면 `피드백 기록` 뒤 다음 시도

평가 자산 경로의 시도는 5·6단계가 `eval-scope`와 `calibrate` 판정으로 바뀐다.

### 7.5 멱등성과 복구 규칙

- 오케스트레이터는 한 번에 이슈 하나만 실행해야 한다. 그래서 중복 선점이 생기지 않는다.
- 선점은 트래커 상태 이동(`Todo` → `In Progress`)으로 기록해야 한다.
- 재시작 복구는 트래커와 파일 시스템으로 해야 한다. 시작할 때 `In Progress`이면서 팩토리 라벨이 있는 이슈는 끊긴 실행으로 보고, 댓글을 남긴 뒤 `Todo`로 되돌린다.
- 작업 공간과 브랜치는 실행이 끝나도 지우지 않는다. 병합한 작업 공간만 지운다. 같은 이슈를 다시 실행하면 같은 작업 공간과 남아 있는 피드백에서 이어 간다.

## 8. 폴링, 선점, 조정

### 8.1 폴링 루프

`up`은 시작할 때 재시작 복구(7.5)를 하고 헤드리스 브라우저를 하나 띄운 뒤, 다음 틱을 반복한다.

1. `Canceled` 이슈의 작업 공간을 정리한다 (8.6).
2. 일시 정지 중이 아니면 후보 하나를 고른다 (8.2). 트래커 조회가 실패하면 이번 틱의 후보는 없다.
3. 후보가 없고 일시 정지 중이 아니면 발견 루프를 부른다. 발견 루프가 이슈를 만들었으면 기다리지 않고 다음 틱으로 간다.
4. 후보가 없으면, `--once`이면 끝내고, 아니면 폴링 간격만큼 기다린다.
5. 후보가 있으면 이슈 하나를 처리한다 (19.3). 처리 결과가 평가 자산 변경이면 자신을 다시 실행한다 (6.3).

### 8.2 후보 선택

후보는 다음을 모두 만족해야 한다.

- 상태가 `Todo`다.
- 라벨에 `factory-eval` 또는 `factory`가 있다. 둘 다 있으면 `factory-eval`(평가 자산 경로)이 우선한다.

정렬 순서:

1. 우선순위 1~4가 먼저, 오름차순 (1이 가장 급하다). 우선순위가 없으면 뒤로 간다.
2. 만든 시각이 오래된 것 먼저.
3. `identifier` 사전순.

### 8.3 동시성

- 현재 구현: 동시에 실행하는 작업은 1개다. 실행 슬롯, 상태별 슬롯, 병합 큐는 없다.
- 동시 실행을 넣는 구현은 제품 main에 대한 병합 충돌과 같은 시드 평가의 공정성을 함께 정의해야 한다.

### 8.4 재시도

- 재시도 단위는 시도다. 작업 하나에서 최대 `MAX_ATTEMPTS`(3)번 시도한다.
- 실패한 시도의 판정과 증거는 `.factory/feedback.json`으로 다음 시도에 넘겨야 한다 (9.3).
- 시간 지연이나 지수 백오프는 없다. 시도는 곧바로 이어진다.
- 시도를 다 쓰면 자동으로 다시 실행해서는 안 된다. `Needs Human`으로 넘긴다.
- 에이전트 프로세스의 비정상 종료(시간 초과 포함)도 시도 하나로 센다. 바뀐 파일이 있으면 커밋하고, 바뀐 것이 없어도 평가는 진행한다.

### 8.5 실행 중 조정

- 시도를 시작하기 전마다 이슈를 다시 읽어야 한다. 이슈가 없거나 `In Progress`가 아니면 실행을 멈추고 상태를 `canceled`로 남긴다. 트래커 상태는 바꾸지 않는다.
- 현재 구현: 시도 도중(에이전트 실행 중, 평가 중)에는 확인하지 않는다. 사람이 상태를 바꿔도 진행 중인 시도는 끝까지 간다.

### 8.6 Canceled 정리

- 틱마다 `Canceled` 이슈의 작업 공간을 두 경로(제품, 평가 자산)에서 찾아 지워야 한다.
- 브랜치는 지우지 않는다. 사람이 나중에 볼 수 있게 하기 위해서다.

### 8.7 일시 정지

오케스트레이터는 다음 중 하나가 참이면 멈춰야 한다.

| 이유 | 조건 |
|---|---|
| 하루 비용 | 오늘 시작한 실행의 `cost_usd` 합이 하루 상한 이상이다 |
| 연속 실패 | 끝난 실행(`done`, `needs_human`)의 마지막 3개가 모두 `needs_human`이다 |

- 멈출 때 트래커에 일시 정지 이슈를 만들어야 한다. 이 이슈에는 팩토리 라벨을 붙이지 않는다 (후보가 되지 않게).
- 멈춰 있는 동안 후보 선택과 발견 루프를 하지 않는다. `Canceled` 정리는 계속한다.
- 사람이 일시 정지 이슈를 `Done`으로 옮기면 풀린다. 풀 때 확인 기록(`pause-ack.json`)을 남겨, 같은 날 비용 상한과 이미 센 연속 실패로 곧바로 다시 멈추지 않게 해야 한다.

## 9. 작업 공간과 안전

### 9.1 배치

```text
var/worktrees/<작업 공간 키>/            제품 경로 (product/ 저장소의 git worktree)
var/worktrees-factory/<키>/              평가 자산 경로 (팩토리 저장소의 git worktree)
  .factory/                              작업 공간 자료 (커밋하지 않는다)
```

### 9.2 생성과 재사용

- 작업 공간이 이미 있으면 그대로 써야 한다.
- 없으면 브랜치가 있으면 그 브랜치로, 없으면 `main`에서 새 브랜치를 만들어 worktree를 만든다.
- 평가 자산 작업 공간에는 `vendor/`를 링크로 넣는다 (git 밖의 파일이다).

### 9.3 작업 공간 자료 (`.factory/`)

| 파일 | 내용 | 경로 |
|---|---|---|
| `work-item.yaml` | 작업 명세 | 둘 다 |
| `CONTRACT.md` | 앱 계약 사본 | 제품 |
| `vendor/` | three.js 사본 | 제품 |
| `fixtures/public-NN/{input.png, gt.json}` | 공개 세트 입력과 정답 | 제품 |
| `feedback.json` | 이전 시도의 판정, 실패 지표와 최악 장면, 점수, 계약 위반, 이전 노트 | 둘 다 |
| `feedback/` | 공개 세트 최악 장면의 뷰어 캡처, 평가 시트, 앱 로그 끝부분 | 제품 |
| `attempt-notes.md`, `refactor-notes.md` | 에이전트의 노트 | 둘 다 |

- 홀드아웃 장면의 정답은 작업 공간에 넣어서는 안 된다.
- 피드백의 장면 이미지는 공개 세트만 준다. 평가 시트에는 홀드아웃 행도 들어 있다 (부록 B).

### 9.4 커밋 규칙

- 팩토리는 시도가 끝날 때마다 작업 공간의 모든 변경을 팩토리 작성자로 커밋해야 한다. 바뀐 것이 없으면 커밋하지 않는다.
- `.factory/`, `__pycache__/`, `*.pyc`는 저장소의 `info/exclude`에 넣어 커밋하지 않아야 한다. 평가 자산 경로는 `.factory/`와 `/vendor`를 뺀다.
- 에이전트는 커밋하지 않는다 (하네스가 알린다).

### 9.5 안전 불변식

- 에이전트는 작업 공간을 현재 폴더로 해서 실행해야 한다.
- 작업 공간 경로는 정해진 루트(`var/worktrees/`, `var/worktrees-factory/`) 아래여야 한다.
- 작업 공간 키는 4.2의 규칙으로 정리한 이름이어야 한다.
- 제품 main과 팩토리 main에는 판정을 통과한 커밋만 병합해야 한다.

## 10. 에이전트 실행 계약

### 10.1 실행 명령

구현, 리팩터링, 평가 자산 에이전트는 Claude Code CLI를 비대화식으로 한 번 실행한다.

```sh
claude -p <지시문> --bare --append-system-prompt <시스템 지시문> \
  --output-format stream-json --verbose \
  --dangerously-skip-permissions --max-budget-usd 5
```

- `--bare`로 사용자 개인 설정(hook, 플러그인, 자동 메모리, CLAUDE.md 자동 탐색)을 끊어야 한다. 실행 결과가 개인 설정에 따라 달라지지 않게 하기 위해서다.
- 모델을 지정하지 않는다. Bedrock 기본값을 쓴다.
- 시도 하나는 에이전트 세션 하나다. 같은 세션에 이어서 턴을 보내지 않는다. 이어 가는 정보는 작업 공간의 파일(피드백, 노트)로 넘긴다.

### 10.2 환경

에이전트와 제품에 넘기는 환경은 다음 규칙으로 만들어야 한다 (`agent_env`).

- `AWS_PROFILE=default`, `AWS_REGION`·`AWS_DEFAULT_REGION`은 default profile의 리전(없으면 `us-east-1`), `CLAUDE_CODE_USE_BEDROCK=1`.
- `ANTHROPIC_MODEL`을 지운다.
- 트래커 비밀값(`LINEAR_API_KEY`)을 지운다.
- 제품을 띄울 때는 `CODEX_HOME=var/codex-home`을 더한다.

### 10.3 결과 해석

stream-json 출력에서 다음을 읽어 시도 기록의 `claude`에 남긴다.

| 필드 | 출처 |
|---|---|
| `model` | `system/init` 이벤트 |
| `cost_usd`, `turns`, `subtype`, `is_error`, `summary` | 마지막 `result` 이벤트 |
| `tool_calls` | `assistant` 이벤트의 `tool_use` 수 |
| `exit`, `seconds` | 프로세스 종료 코드(시간 초과면 `"timeout"`), 걸린 시간 |

출력 전체는 실행 폴더의 `attempt-<N>.jsonl`(리팩터링은 `refactor.jsonl`)에 남겨야 한다.

### 10.4 구조화 호출

판단만 하고 파일을 고치지 않는 호출은 빈 임시 폴더에서 JSON 스키마와 함께 부른다.

| 호출 | 스키마 | 비용 상한 | 시간 상한 |
|---|---|---|---|
| 작업 명세 변환 (12절) | `goal, keep, drop, constraints, notes, metrics` | $0.5 | 300초 |
| 발견 관찰 (15.5) | `observation, hypothesis` | $0.3 | 300초 |

결과는 `structured_output`에서 읽어야 한다.

### 10.5 승인 정책

- 현재 구현: 승인 요청이 없는 고신뢰 설정이다 (`--dangerously-skip-permissions`). 에이전트는 실행 사용자의 권한으로 머신 전체에 접근할 수 있다.
- 이 설정을 쓰는 구현은 신뢰할 수 있는 단일 머신에서만 실행해야 한다 (18.1).

### 10.6 시간 초과와 오류

| 상황 | 동작 |
|---|---|
| 시간 상한(3600초) 초과 | 프로세스를 끝내고 `exit="timeout"`으로 기록한다. 시도는 평가까지 진행한다 |
| 비용 상한 도달 | Claude Code가 스스로 멈춘다 (`subtype`에 남는다). 시도는 평가까지 진행한다 |
| 구조화 호출 실패 | 작업 명세 변환은 실패로 보고 사람에게 넘긴다. 발견 관찰은 빈 관찰로 이슈를 만든다 |

## 11. 트래커 연동 계약

### 11.1 필수 연산

| 연산 | 뜻 |
|---|---|
| `fetch_issues(states)` | 팀의 이슈 중 상태가 목록 안에 있는 것 (최대 100개) |
| `fetch_issue(id)` | 이슈 하나. 없으면 null |
| `move(issue, state)` | 상태 이름으로 이동 |
| `comment(issue, body)` | 팩토리 표시를 붙여 댓글 |
| `upload(path)` | 이미지를 트래커 저장소에 올리고 주소를 돌려준다. 실패하면 null |
| `create_issue(title, body, labels, state)` | 이슈 생성 (기본 상태 `Todo`) |
| `setup_team`, `check_team` | 라벨 3개(`factory`, `factory-eval`, `auto-discovered`)와 `Needs Human` 상태를 만들고 확인한다 |

### 11.2 정규화

- 이슈는 4.1.1의 형식으로 정규화해야 한다.
- 라벨은 소문자로 비교해야 한다.
- 댓글은 오래된 순으로 정렬해야 한다.

### 11.3 쓰기 경계

- 트래커 쓰기는 팩토리 컨트롤러만 해야 한다. 에이전트와 제품 프로세스는 트래커 자격 증명을 받아서는 안 된다 (10.2).
- 이 경계 덕분에 이슈 본문에 섞인 지시가 에이전트를 통해 트래커를 바꿀 수 없다. 단, 파일 접근은 막지 않는다 (18.2).

### 11.4 보고 형식

팩토리는 이슈를 처리하는 동안 다음 댓글을 써야 한다.

| 시점 | 내용 |
|---|---|
| 선점 | 대상(`product`/`eval`), 작업 공간 이름 |
| 명세 변환 뒤 | 작업 명세 YAML (밑줄 키와 `human_notes` 제외) |
| 시도마다 | 통과 여부, 커밋, 조건표, 점수와 비용 |
| 완료 | 병합 커밋, 배포 주소, 점수, 리팩터링 결과, 비용 합계, 평가 시트 첨부 |
| Needs Human | 최고점 시도의 조건표와 브랜치, 에이전트 노트 끝부분, 다음 행동 세 가지, 평가 시트 첨부 |

Needs Human 댓글의 다음 행동:

- 구현 방향이 틀렸다 → 힌트 댓글을 달고 `Todo`로 옮긴다. 보존한 작업 공간에서 이어서 실행한다.
- 요구사항이 모호하거나 크다 → 새 이슈를 만들고 이 이슈는 `Canceled`로 옮긴다.
- eval이 틀렸다 → `factory-eval` 이슈를 만들고, 고친 뒤 이 이슈를 다시 `Todo`로 옮긴다.

첨부 업로드 실패와 댓글 실패는 실행 결과를 바꿔서는 안 된다.

### 11.5 오류 처리

- 후보 조회가 실패하면 그 틱만 건너뛰어야 한다.
- 처리 도중의 트래커 오류(상태 이동 실패 등)는 현재 구현에서 오케스트레이터를 끝낸다. 다시 시작하면 7.5의 복구가 이슈를 `Todo`로 되돌린다.

## 12. 작업 명세 변환 (의도 → 명세)

### 12.1 입력

- 이슈 제목, 본문, 사람 댓글(팩토리 표시가 없는 댓글, 오래된 순).
- 지표 키 목록과 방향, 최종 목표 (`viewer.console_errors` 제외).

### 12.2 LLM의 역할

LLM은 자연어를 구조로 정리만 해야 한다. 목표를 지어내서는 안 되고, 코드는 읽지 않는다. `goal`에는 구현 방법을 적지 않는다.

### 12.3 지표 목표 검증

LLM이 낸 지표 목표마다 프로그램이 다음을 확인해야 한다. 하나라도 어기면 변환은 실패다.

1. 부등호 방향이 그 지표의 최종 목표 방향과 같다. 기준을 거꾸로 느슨하게 만드는 길을 막는다.
2. 인용 구절이 이슈 원문(제목, 본문, 사람 댓글)에 그대로 있다.
3. 인용 구절에 목표 숫자가 들어 있다.

안정성 지표(`stability.*`)는 홀드아웃에만 있으므로 세트를 홀드아웃으로 고정한다.

### 12.4 수용 조건 조립

| 대상 | 수용 조건 |
|---|---|
| 제품 | `run`, `contract-smoke`, `{process: "codex app-server", absent: "codex exec"}`, 검증한 지표 목표(세트별), `holdout-regression` |
| 평가 자산 | `calibrate`, `eval-scope` |

- 기본 조건은 이슈 내용과 무관하게 항상 붙여야 한다.
- 숫자가 없는 요구사항은 `keep`, `constraints`, `notes`로만 에이전트에게 전달된다. 수용 조건으로 강제되지 않는다 (부록 B).
- 평가 자산 경로는 지표 목표를 받지 않는다.

### 12.5 실패

변환이 실패하면 에이전트를 실행하지 않고, 이유와 다음 행동을 댓글로 남긴 뒤 `Needs Human`으로 옮겨야 한다.

## 13. 평가 계약 (Oracle)

평가기는 정답을 아는 합성 장면을 만들고, 그 장면을 렌더링한 이미지를 제품에 넣고, 제품 뷰어가 그린 장면 그래프를 정답과 비교한다. 이 절의 모든 동작은 평가 자산이다.

### 13.1 평가 세트

| 세트 | 장면 | 시드 | 렌더링 | 에이전트에게 |
|---|---|---|---|---|
| 공개 | 4 | 고정 (`PUBLIC_SEEDS`) | 그림자·노이즈 없음 | 이미지와 정답 공개 |
| 홀드아웃 | 8 | 평가마다 새로 뽑는다 (10,000~999,999) | 그림자, 질감 노이즈, 이미지 노이즈, 넓은 조명·롤 | 공개하지 않는다 |

- 장면 생성은 시드에 대해 결정적이어야 한다. 같은 시드와 변형은 부동소수점 값까지 같은 장면을 만든다.
- 장면의 물체는 서로 겹치지 않고, 카메라에 보이고, 화면 안 실루엣의 절반 이상(`MIN_VISIBLE_FRACTION`)이 다른 물체에 가려지지 않아야 한다.
- 측정 잡음을 재거나 비교할 때는 홀드아웃 시드를 고정할 수 있다 (`eval --holdout-seeds`).

### 13.2 렌더링

- 헤드리스 Chromium(SwiftShader WebGL2) 하나에서 three.js 사본으로 정답 장면을 640×480으로 렌더링한다.
- 같은 시드를 두 번 렌더링하면 같은 PNG여야 한다 (채점기 검증의 결정성 항목).

### 13.3 요청과 측정

1. 모든 장면의 첫 요청을 먼저, 두 번째 요청(홀드아웃)을 뒤에 한 묶음으로 보낸다.
2. 상태를 2초마다 확인한다. 현재 구현: 묶음 전체에 하나의 마감(대기 시작부터 900초)을 둔다. 마감까지 끝나지 않은 요청은 `timeout`이다.
3. `done`이 된 요청마다 장면 파일이 있고 `build`가 보이는지 확인한다.
4. 뷰어를 640×480으로 연다 (불러오기 60초, `__PREVIZ__` 30초, `ready` 60초). `role`이 붙은 메시와 카메라를 읽는다.
5. 원근 카메라와 `ground` 메시가 있으면 정답과 비교해 지표와 충실도를 계산한다.
6. 받지 못하거나 측정하지 못한 장면은 최악값(`WORST_METRICS`)과 충실도 0점으로 센다.

### 13.4 채점

지표와 최종 목표 (`FINAL_TARGETS`):

| 지표 키 | 측정 | 최종 목표 |
|---|---|---|
| `camera.fov_error_deg` | 수직 FOV 차 | ≤ 5 |
| `camera.pitch_error_deg`, `camera.roll_error_deg` | 피치·롤 차 | 각 ≤ 3 |
| `objects.f1` | 카메라 높이 1배 이내로 짝지은 물체 검출 F1 | ≥ 0.8 |
| `objects.position_error` | 짝지은 물체의 바닥면 위치 오차 ÷ 방 대각선 | ≤ 0.10 |
| `objects.size_error` | 크기 상대 오차 | ≤ 0.20 |
| `objects.color_delta_e` | 대표 색 ΔE (CIE76) | ≤ 15 |
| `reprojection.iou` | 같은 카메라로 다시 그린 물체 실루엣의 IoU | ≥ 0.7 |
| `viewer.console_errors` | 콘솔 오류와 페이지 예외 수 | ≤ 0 |
| `stability.fidelity_delta` | 두 요청 결과의 충실도 차 (홀드아웃) | ≤ 0.5 |
| `stability.fov_delta_deg` | 두 요청 결과의 FOV 차 (홀드아웃) | ≤ 0.2 |
| `run.seconds` | 요청부터 done까지 | 목표 없음 (기록만) |

모든 위치와 크기는 카메라 높이로 나눠 비교해야 한다.

충실도:

```text
fidelity = 100 × F1 × (0.20·화각 + 0.15·기울기 + 0.20·배치 + 0.15·(크기·색) + 0.30·IoU)
  화각    = clamp(1 − 화각 오차/30)
  기울기  = clamp(1 − (피치 오차 + 롤 오차)/2/15)
  배치    = clamp(1 − 위치 오차/0.5)
  크기·색 = (clamp(1 − 크기 오차) + clamp(1 − ΔE/60)) / 2
  clamp(x) = min(1, max(0, x))
```

세트 요약의 지표와 충실도는 장면 평균이다.

### 13.5 안정성

- 홀드아웃 장면은 두 번 요청해야 한다. 두 번째 이미지는 첫 이미지의 채널마다 ±2 이내의 정수 노이즈를 더해 다시 인코딩한 PNG다. 바이트가 달라야 하고, 같은 장면은 항상 같은 바이트여야 한다 (노이즈 시드는 장면 시드에서 정한다). 그래서 후보와 main이 같은 두 번째 입력을 받는다.
- 두 결과를 각각 채점하고, 첫 요청 행에 `stability.fidelity_delta`(충실도 차 절댓값)와 `stability.fov_delta_deg`(FOV 차 절댓값)를 붙인다.
- 두 번째 결과를 채점하지 못하면 충실도 0점으로, 어느 한쪽이라도 FOV를 잴 수 없으면 FOV 차 90°로 센다.
- 충실도, 다른 지표, 회귀 비교는 첫 요청 결과로만 계산해야 한다.

### 13.6 계약 스모크

평가 중 다음 위반을 모아야 한다. 하나라도 있으면 `contract-smoke` 실패다.

| 항목 | 위반 |
|---|---|
| `post` | `POST /api/requests`가 `202 {"key"}`가 아니다 |
| `status` | 상태 응답이 `pending`, `done`, `failed`가 아니다 |
| `scene_file` | 장면 파일이 없거나 `build`가 없다 |
| `viewer` | 뷰어를 측정하지 못했거나 카메라가 원근 카메라가 아니다 |
| `ground_mesh` | `role=ground` 메시가 없다 |
| `local_only` | 뷰어가 `localhost`·`127.0.0.1` 밖으로 요청했다 (평가기가 막고 기록한다) |
| `done` | done이 된 요청이 하나도 없다 |

### 13.7 프로세스 관찰

- 제품은 새 프로세스 그룹으로 띄워야 한다. 평가기는 대기하는 동안 그 그룹의 프로세스 명령줄(실행 파일 이름과 인자 앞 4개)을 모은다.
- `process` 조건의 패턴은 첫 단어로 시작하는 실행 파일 뒤에 나머지 단어가 차례로 나오면 맞는다 (예 `codex app-server`는 `node .../codex app-server`에도 맞는다).

### 13.8 코드 지표

| 지표 | 정의 | 기준 |
|---|---|---|
| `code.max_file_lines` | 파일 하나의 코드 줄 수 (빈 줄과 `#` 주석 줄 제외) | 400 |
| `code.max_function_lines` | 함수 하나의 줄 수 (정의부터 끝까지) | 60 |
| `code.max_complexity` | 1 + 분기 노드 수(`if`, `for`, `while`, `except`, 조건식, 컴프리헨션, `match` 분기, `assert`) + 불리언 연산의 추가 피연산자 | 15 |

- 대상은 폴더 아래 `.py` 파일이다. `.factory`, `.git`, `.venv`, `vendor`, `static`, `data`, `node_modules`, `__pycache__`, `var`, `product`는 뺀다.
- 결과에는 지표마다 가장 나쁜 파일·함수 하나를 함께 남긴다.

### 13.9 수용 조건 판정

| 조건 | 판정 |
|---|---|
| `run` | 앱이 180초 안에 포트를 열었다 |
| `contract-smoke` | 위반이 없다. 앱이 시작되지 않았으면 실패 |
| `process` / `absent` | 관찰한 명령에 `process`가 있고 `absent`가 없다. 관찰하지 못했으면 실패 |
| 세트 지표 | 후보의 세트 평균이 목표를 채운다. 값이 없으면 실패. 실패하면 최악 장면 3개를 증거로 남긴다 |
| `holdout-regression` | 후보 홀드아웃 충실도 ≥ 같은 시드 main − 2.0. main이 없으면 통과로 보고 생략을 기록한다 |
| `eval-scope` | merge-base 이후 바뀐 파일이 하나 이상이고 모두 `tsf/oracle/` 또는 `CONTRACT.md`다 |
| `calibrate` | 작업 공간에서 `uv run factory.py calibrate`가 0으로 끝난다 (1800초 상한) |

- 알 수 없는 조건은 오류로 멈춰야 한다.
- 후보는 공개 + 홀드아웃 전체로 평가하고, main은 같은 홀드아웃 장면(같은 시드, 같은 두 번째 입력)으로만 평가해야 한다.

### 13.10 채점기 검증 (`calibrate`)

기준 앱으로 채점기가 좋은 결과와 나쁜 결과를 구분하는지 확인한다. 기준 앱은 평가기가 등록한 정답을 쓰는 검증 전용 앱이다.

| 기준 앱 | 동작 |
|---|---|
| `gt` | 정답 장면을 그대로 돌려준다 |
| `empty` | 바닥과 벽만 돌려준다 |
| `noise-0.0`, `0.1`, `0.2`, `0.4` | 정답에 그만큼 오차를 더한다 (결정적) |
| `jitter` | 요청마다 0~20%에서 오차 수준을 새로 뽑는다 (같은 장면도 흔들린다) |

통과 조건 (모두):

| 항목 | 기준 |
|---|---|
| 결정성 | 같은 시드 두 번 렌더링 → 같은 PNG |
| 계약 스모크 | 기준 앱 7개 모두 위반 없음 |
| 두 번째 요청 입력 | 바이트가 다르고, 채널 최대 차 1~2, 다시 만들면 같은 바이트 |
| 정답 앱 | 95점 이상 |
| 빈 앱 | 5점 이하 |
| 오차 주입 앱 | 오차 0%가 95점 이상이고, 오차가 커질수록 점수가 줄어든다 |
| 안정성 (결정적 앱) | 결정적 기준 앱 6개의 안정성 지표가 모두 0 |
| 안정성 (흔들리는 앱) | 흔들리는 앱의 두 안정성 지표가 최종 목표를 넘는다 |

### 13.11 평가 기록

```text
var/evals/<YYYYmmdd-HHMMSS>-<라벨>/        eval, calibrate, discover
var/runs/<실행 ID>/attempt-<N>-eval/        생산 시도 (candidate/, main/, *-app.log)
  summary.json                              평가 요약 (4.1.7)
  sheet.png                                 평가 시트: 장면마다 입력·뷰어 캡처·지표
  <장면>/input.png, gt.json                  입력과 정답
  <장면>/viewer.png, measured.json, result.json
  <장면>/input-repeat.png, viewer-repeat.png, measured-repeat.json, result-repeat.json   (홀드아웃)
  data/, app.log                            앱의 데이터 폴더와 로그
```

## 14. 수용과 전달

### 14.1 제품 경로

1. 시도가 eval을 통과하면 더 시도하지 않는다.
2. 리팩터링 단계를 거친다 (14.2).
3. 리팩터링을 받아들였으면 그 커밋을, 아니면 통과한 시도의 커밋을 병합·배포한다 (14.3).
4. 배포까지 끝나면 `done`, 배포가 실패하면 `needs_human`이다.

### 14.2 리팩터링 단계

- 통과한 구현의 코드 지표가 `CODE_TARGETS` 안이면 건너뛰고 그 사실을 기록해야 한다.
- 넘으면 리팩터링 에이전트를 한 번 실행하고, 다음 셋을 모두 만족할 때만 받아들여야 한다.

| 검사 | 조건 |
|---|---|
| `behavior` | 작업 명세의 수용 조건을 다시 모두 통과한다 (같은 평가를 다시 한다) |
| `fidelity` | 공개 세트 충실도가 리팩터링 전보다 2.0점 넘게 낮지 않다 |
| `structure` | 기준을 넘었던 지표 중 하나 이상이 줄고, 모든 지표가 `max(이전 값, 기준)` 이하다 |

- 받아들이지 않으면 작업 공간을 통과한 시도의 커밋으로 되돌리고 그대로 병합해야 한다. 구현은 이미 eval을 통과했기 때문이다.
- 리팩터링 단계는 다시 시도하지 않는다.
- `refactor` 명령은 이미 병합된 main에 이 단계만 실행한다. 수용 조건은 `run`, `contract-smoke`, `holdout-regression`이고, 공개 세트 기준은 마지막으로 병합된 실행의 점수다.

### 14.3 병합과 배포

1. 작업 공간 브랜치를 병합할 커밋으로 맞춘다.
2. 제품 main에 squash 병합하고, 팩토리 작성자로 `<작업 ID>: <goal 앞 72자>` 커밋을 만든다. 본문에 후보 커밋을 적는다.
3. 로컬 프로덕션 서버를 내리고 main으로 다시 띄운다 (`PROD_PORT`). `GET /api/requests/not-a-key`가 200이나 404로 응답해야 배포 성공이다.
4. 배포가 실패하면 main을 병합 전 커밋으로 되돌리고 이전 버전을 다시 띄운다.
5. 성공하면 작업 공간을 지운다. 브랜치는 남긴다.

### 14.4 평가 자산 경로

1. 팩토리 저장소의 작업 공간에서 평가 자산 에이전트를 실행하고 커밋한다.
2. `eval-scope`가 통과하면 `calibrate`를 작업 공간의 코드로 돌린다.
3. 둘 다 통과하면 팩토리 main에 squash 병합한다. 팩토리 저장소의 평가 자산에 커밋하지 않은 변경이 있으면 병합해서는 안 된다.
4. 병합하면 작업 공간을 지우고, 오케스트레이터는 자신을 다시 실행한다.
5. 통과하지 못하면 판정을 피드백으로 남기고 다음 시도로 간다. 시도를 다 쓰면 `needs_human`이다.

### 14.5 포기

시도를 다 쓰면 다음을 해야 한다.

- 병합하지 않는다.
- 공개 세트 충실도가 가장 높은 시도의 커밋으로 작업 공간을 되돌려, 브랜치 끝에 최고점 결과를 남긴다.
- 에이전트 노트를 실행 폴더로 옮긴다.
- 트래커에 최고점 시도와 다음 행동을 보고하고 `Needs Human`으로 옮긴다.

## 15. 발견 루프

### 15.1 언제 도는가

- 대기열이 비었고 일시 정지 중이 아닐 때 오케스트레이터가 부른다. `discover` 명령으로 직접 부를 수도 있다.
- 제품 main 커밋이나 평가 자산의 마지막 커밋이 지난 발견 평가와 다를 때만 평가해야 한다 (`--force`면 다시). 같은 main을 같은 기준으로 다시 평가해도 새로 알 것이 없기 때문이다.
- 열린 자동 이슈(`auto-discovered`, 상태 Backlog·Todo·In Progress·Needs Human)가 3개 이상이면 평가하지 않는다.

### 15.2 미달 판정

1. main을 공개 + 새 홀드아웃으로 평가한다.
2. 세트마다 최종 목표를 채우지 못한 지표를 찾는다. 두 세트 모두 미달이면 홀드아웃 쪽 하나로 낸다.
3. 목표와의 차이가 그 지표의 측정 잡음(`METRIC_NOISE`) 이하이면 이슈를 만들지 않고 "잡음 안"으로 기록해야 한다.

측정 잡음은 같은 main을 고정 시드로 세 번 평가한 홀드아웃 평균의 범위다 (2026-11-24, 8장면 × 2요청).

| 지표 | 잡음 | 지표 | 잡음 |
|---|---|---|---|
| 홀드아웃 충실도 | 0.79 | `objects.position_error` | 0.004 |
| `camera.fov_error_deg` | 0.985 | `objects.size_error` | 0.003 |
| `camera.pitch_error_deg` | 0.008 | `objects.color_delta_e` | 0.309 |
| `camera.roll_error_deg` | 0.028 | `reprojection.iou` | 0.005 |
| `objects.f1` | 0.000 | `stability.fidelity_delta` | 0.979 |
| | | `stability.fov_delta_deg` | 0.749 |

### 15.3 단계적 목표

다음 목표는 현재값과 최종 목표의 중간이다. 최종 목표를 한 번에 요구하면 통과하지 못하고 사람에게 넘어가기 때문이다.

```text
step = max(|최종 − 현재| / 2, 잡음)
next = 현재 − step  (방향 <=)   또는   현재 + step  (방향 >=)
next는 최종 목표를 넘지 않게 자른다
|next − 최종| < max(|최종| × 0.1, 0.05) 이면 next = 최종
|최종| < 1 이면 소수 둘째 자리, 아니면 첫째 자리로 반올림
```

### 15.4 이슈 생성 규칙

- 한 번에 최대 2개, 열린 자동 이슈와 합쳐 최대 3개까지 만든다.
- 같은 지표의 열린 자동 이슈가 있으면 만들지 않는다. 중복은 본문의 `지표 키: \`<세트>:<지표>\`` 줄로 확인한다.
- 라벨은 `factory`, `auto-discovered`, 상태는 `Todo`다.
- 제목: `[auto] <지표 이름> 개선: <세트> <현재> → <다음 목표> (최종 목표 <최종>)`.
- 본문: 근거(평균, 최종 목표, 평가 기록), 최악 장면 3개 표, 관찰, 원하는 결과(다음 목표를 숫자로), 측정 잡음, 제약, 지표 키 줄.
- 다음 목표는 본문에 숫자로 적어야 한다. 그래야 작업 명세 변환의 지표 검증(12.3)을 통과한다.
- 발견 루프가 만든 이슈는 사람이 만든 이슈와 같은 작업 명세 규칙과 eval 관문을 거쳐야 한다.

### 15.5 관찰 에이전트

- 이슈마다 장면별 수치를 받아 관찰 한 문장과 원인 가설 한 문장을 쓴다. 해결 방법은 쓰지 않는다. 이슈는 목표를 말하고, 방법은 구현 에이전트가 정한다.
- 미달 판정과 이슈 생성 규칙은 프로그램이 정한다. 관찰 에이전트는 미달 지표가 있을 때만 부른다.

### 15.6 상태

평가가 끝나면 `var/discovery.json`에 제품 커밋, 평가 자산 커밋, 평가 기록, 시각, 미달 지표, 잡음 안 지표, 만든 이슈, 중복으로 건너뛴 지표를 써야 한다.

## 16. 관측과 기록

| 출처 | 내용 | 독자 |
|---|---|---|
| 표준 출력 | `[작업 ID]`를 앞에 붙인 진행 줄 (시도, 조건별 통과·실패, 비용, 결과) | 운영자 |
| `var/runs/<실행 ID>/run.json` | 실행 기록 (4.1.5) | 운영자, 다음 단계 |
| `var/runs/<실행 ID>/attempt-<N>.jsonl` | 에이전트의 전체 이벤트 | 디버깅, 학습 루프 |
| `var/runs/<실행 ID>/*notes.md` | 에이전트 노트 (병합 전에 작업 공간에서 옮긴다) | 사람 |
| `var/evals/…/summary.json`, `sheet.png` | 평가 요약과 평가 시트 | 사람, 에이전트 |
| Linear 댓글 | 11.4의 보고 | 사람 |
| `metrics` 명령 | 코드 지표 | 사람 |

- 실행 기록은 실행이 끝날 때뿐 아니라 시도마다 저장하는 것을 권장한다 (현재 구현: 제품 경로는 시도마다, 평가 자산 경로는 끝에 저장한다).
- 현재 구현에는 HTTP 상태 API와 대시보드가 없다. 운영 화면은 Linear다.

## 17. 실패 모델과 복구

### 17.1 실패 종류

| 실패 | 동작 |
|---|---|
| 트래커 조회 실패 | 그 틱을 건너뛴다 |
| 처리 중 트래커 쓰기 실패 | 오케스트레이터가 끝난다. 다시 시작하면 끊긴 이슈를 `Todo`로 되돌린다 |
| 작업 명세 변환 실패 | 실행하지 않고 `Needs Human` |
| 앱이 시작되지 않는다 | `run` 실패로 판정하고 다음 시도 |
| 요청 시간 초과, 측정 실패 | 해당 장면을 최악값으로 채점 |
| 에이전트 시간·비용 초과 | 시도 하나로 세고 평가까지 진행 |
| 시도 소진 | 포기 (14.5) |
| main을 띄우지 못한다 | 회귀 비교를 생략하고 기록한다 |
| 배포 실패 | main을 되돌리고 이전 버전을 띄운 뒤 `Needs Human` |
| 평가 시트·첨부·댓글 실패 | 기록만 하고 결과는 바꾸지 않는다 |
| 하루 비용 상한, 연속 Needs Human | 일시 정지 (8.7) |

### 17.2 재시작 복구

- 메모리 상태는 복원하지 않는다. 복원할 것이 없다.
- 시작할 때 `In Progress`인 팩토리 이슈를 `Todo`로 되돌리고, 남아 있는 작업 공간에서 이어서 실행한다.
- 일시 정지 상태는 파일(`pause.json`, `pause-ack.json`)에서 이어진다.
- 발견 루프의 중복 평가 방지는 `discovery.json`에서 이어진다.

### 17.3 사람의 개입 지점

| 지점 | 사람이 하는 일 |
|---|---|
| 이슈 등록 | 제품 요구사항은 `factory`, 평가 기준 변경은 `factory-eval` |
| `Needs Human` | 힌트를 달고 `Todo`, 새 이슈와 `Canceled`, 또는 `factory-eval` 이슈 |
| 실행 중 | 이슈를 `In Progress` 밖으로 옮겨 다음 시도 전에 멈춘다 |
| 일시 정지 이슈 | 원인을 확인하고 `Done`으로 옮긴다 |
| 최종 목표 | `factory-eval` 이슈로 목표를 바꿔 발견 루프의 방향을 정한다 |

## 18. 보안과 운영 안전

### 18.1 신뢰 경계

- 팩토리는 신뢰할 수 있는 단일 머신에서, 신뢰할 수 있는 이슈 작성자만 있는 트래커와 함께 실행해야 한다.
- 에이전트는 승인 없이 실행 사용자의 권한으로 동작한다. 컨테이너나 VM 격리는 없다.
- 이 전제를 바꾸는 구현은 샌드박스, 승인 정책, 네트워크 제한을 함께 정의해야 한다.

### 18.2 비밀값

- 트래커 API 키는 `.env`에 두고 git에 넣어서는 안 된다.
- 에이전트와 제품의 환경에서 트래커 비밀값을 지워야 한다 (`SECRET_ENV_NAMES`). `doctor`가 확인한다.
- 현재 구현의 한계: 에이전트는 같은 사용자로 실행되므로 저장소 루트의 `.env`를 파일로 읽을 수 있다. 환경 변수에서 지우는 것은 비밀값이 실수로 넘어가는 길을 막을 뿐이다 (부록 B).

### 18.3 제품 런타임 격리

- 제품은 사용자 Codex 설정이 아니라 전용 `CODEX_HOME`을 써야 한다. 전용 설정에는 사용자 설정의 모델 공급자와 카탈로그만 옮기고, 모델은 `CODEX_MODEL`로 고정한다.
- 전용 설정은 `plugins`, `remote_plugin`, `plugin_sharing`, `apps`, `browser_use`, `hooks` 기능을 꺼야 한다. 꺼지지 않으면 Codex가 평가 중에 외부 저장소에 접속하는 것이 관찰됐다.
- 에이전트 실행은 `--bare`로 개인 설정을 끊어야 한다.

### 18.4 네트워크

- 평가기는 뷰어가 로컬이 아닌 주소로 보내는 요청을 막고 계약 위반으로 기록해야 한다.
- 제품 프로세스 자체의 네트워크는 막지 않는다 (Codex가 Bedrock을 부른다). 대신 프로세스 관찰로 런타임 요구사항을 확인한다.

### 18.5 비용

- 시도마다 Claude Code 비용 상한과 시간 상한을 둬야 한다.
- 하루 비용 상한에 닿으면 멈춰야 한다.
- 현재 구현의 한계: 하루 비용은 실행 기록의 Claude Code 비용만 센다. 작업 명세 변환, 발견 관찰, 제품의 Codex 비용은 세지 않는다.

## 19. 참조 알고리즘

### 19.1 시작 (`up`)

```text
function up(once, interval):
  load_dotenv()
  for issue in tracker.fetch_issues(["In Progress"]) where target_of(issue):
    tracker.comment(issue, "팩토리가 다시 시작돼 실행이 끊겼다 …")
    tracker.move(issue, "Todo")
  lab = start_headless_browser()
  loop:
    tick(lab, once, interval)
```

### 19.2 틱

```text
function tick(lab, once, interval):
  try:
    sweep_canceled()
    issue = null if paused() else first(sorted_candidates())
  except tracker_error:
    log("Linear 조회 실패, 다음 틱에 다시 시도"); issue = null

  if issue is null and not paused() and discover(lab) is not empty:
    return                                   # 만든 이슈를 다음 틱에 바로 처리한다
  if issue is null:
    if once: exit
    sleep(interval); return

  if process(issue, lab):                    # 평가 자산이 바뀌었다
    lab.close()
    exec_self_with_same_args()
```

### 19.3 이슈 하나 처리

```text
function process(issue, lab) -> reload:
  target = "eval" if "factory-eval" in issue.labels else "product"
  tracker.move(issue, "In Progress")
  tracker.comment(issue, "실행 시작 …")
  try:
    item = issue_to_work_item(issue, target)          # 12절
  except SpecError as e:
    tracker.comment(issue, e + NEXT_ACTIONS)
    tracker.move(issue, "Needs Human")
    return false
  tracker.comment(issue, yaml(public_fields(item)))

  still_active = () -> tracker.fetch_issue(issue.id).state == "In Progress"
  on_attempt = (entry) -> try tracker.comment(issue, attempt_report(entry))

  record = run_eval_change(item, on_attempt, still_active) if target == "eval"
           else run_work_item(item, lab, on_attempt, still_active)
  report_final(issue, record)                          # Done 또는 Needs Human으로 옮긴다
  return record.reload
```

### 19.4 제품 작업 실행

```text
function run_work_item(item, lab, on_attempt, should_continue):
  run = new_run(item)                                  # 실행 폴더, 작업 공간(있으면 재사용)
  write_context(run.worktree, item, lab)               # .factory/
  best = null
  for attempt in 1..MAX_ATTEMPTS:
    if should_continue and not should_continue():
      run.status = "canceled"; return run.finish()
    prompt = render("implement-task", item, attempt, hint(feedback_exists))
    claude = run_claude(run.worktree, prompt, render("implement-system"))
    commit = commit_all(run.worktree, "<id> attempt <n>")
    candidate, baseline = evaluate(run.worktree, main, same_holdout)
    verdict = judge(item.acceptance, candidate, baseline)
    record(attempt); on_attempt(attempt)
    best = better(attempt, best)                       # 공개 세트 충실도 기준
    if verdict.passed:
      run.refactor = refactor_stage(run, attempt)      # 14.2
      final = run.refactor.accepted_commit or commit
      run.delivery = merge_and_deploy(item, run, final)
      run.status = "done" if run.delivery.deployed else "needs_human"
      return run.finish()
    write_feedback(run.worktree, verdict, candidate, baseline)
  give_up(run, best)                                   # 14.5
  return run.finish()
```

### 19.5 리팩터링 단계

```text
function refactor_stage(run, passing, passing_commit, lab):
  before = measure_code(run.worktree)
  over = [k for k in CODE_TARGETS if before[k] > CODE_TARGETS[k]]
  if over is empty: return {ran: false}
  claude = run_claude(run.worktree, render("refactor-task", before, …), render("refactor-system"))
  commit = commit_all(run.worktree, "<id> refactor")
  if commit is null: return {ran: true, accepted: false}
  after = measure_code(run.worktree)
  stage = evaluate_and_judge(run)                      # 같은 수용 조건
  checks = {behavior: stage.passed,
            fidelity: stage.public >= passing.public - PUBLIC_TOLERANCE,
            structure: no_worse(before, after) and any(after[k] < before[k] for k in over)}
  if not all(checks): git_reset_hard(run.worktree, passing_commit)
  return {ran: true, accepted: all(checks), accepted_commit: commit if all(checks)}
```

### 19.6 평가 자산 변경

```text
function run_eval_change(item, on_attempt, should_continue):
  wt = factory_worktree(item.work_item)                # main에서 갈라진 factory-eval/<키>
  for attempt in 1..MAX_ATTEMPTS:
    if should_continue and not should_continue(): return canceled
    run_claude(wt, render("eval-task", …), render("eval-system"))
    commit_all(wt, "<id> eval attempt <n>")
    files = changed_since_merge_base(wt, "main")
    scope_ok = files not empty and all(f startswith ("tsf/oracle/", "CONTRACT.md"))
    calibrate_ok = scope_ok and run("uv run factory.py calibrate", cwd=wt) == 0
    on_attempt(attempt)
    if scope_ok and calibrate_ok:
      squash_merge_into_factory_main(wt)               # 평가 자산에 미커밋 변경이 있으면 거부
      return {status: "done", reload: true}
    write_feedback(wt, conditions, files)
  return {status: "needs_human"}
```

### 19.7 발견

```text
function discover(lab, force):
  commit = product_main_head(); oracle = last_commit_touching("tsf/oracle", "CONTRACT.md")
  if not force and state.commit == commit and state.oracle == oracle: return []
  open_auto = tracker.fetch_issues(OPEN_STATES) where "auto-discovered" in labels
  if len(open_auto) >= MAX_OPEN_AUTO: return []
  summary = evaluate(main, public + new_holdout)
  found, within_noise = gaps(summary, METRIC_NOISE)    # 15.2
  created = []
  for gap in found:
    if len(created) >= MAX_NEW_PER_RUN or len(open_auto) + len(created) >= MAX_OPEN_AUTO: break
    if marker(gap) in any open_auto.description: continue
    seen = observe(summary, gap)                        # 관찰 한 문장, 가설 한 문장
    created += tracker.create_issue(title(gap), body(gap, summary, seen),
                                    labels=["factory", "auto-discovered"])
  save_state(commit, oracle, found, within_noise, created)
  return created
```

## 20. 검증 매트릭스

이 절은 팩토리의 각 부분을 무엇으로 확인하는지 적는다. 현재 저장소에는 단위 시험 묶음이 없다. 확인은 명령과 실제 실행으로 한다.

| 대상 | 확인 방법 | 자동 |
|---|---|---|
| 실행 환경 | `doctor` (6.4) | 예 |
| 트래커 준비와 쓰기 | `linear-setup`이 시험 이슈를 만들고 댓글을 쓴 뒤 지운다 | 예 |
| 채점기 | `calibrate` (13.10). 평가 자산 변경의 수용 조건이기도 하다 | 예 |
| 장면 생성의 결정성 | `calibrate`의 결정성 항목 (렌더링 기준) | 예 |
| 제품 변경 | 작업 명세의 수용 조건 (13.9) | 예 |
| 리팩터링의 동작 보존 | 같은 수용 조건 재통과 + 공개 세트 허용폭 (14.2) | 예 |
| 평가 자산 리팩터링의 동작 보존 | 바꾸기 전 장면·호출 기록과 비트 단위 비교 (SWF-13에서 사람이 함) | 아니오 |
| 팩토리 코드 리팩터링의 동작 보존 | 같은 입력으로 전후 출력 비교 (3단계, SWF-13 뒤 정리에서 사람이 함) | 아니오 |
| 측정 잡음 | 같은 main을 고정 시드로 세 번 평가 (`eval --holdout-seeds`) | 아니오 |
| 비밀값 격리 | `doctor`의 비밀값 격리 항목 | 예 |
| 코드 지표 | `metrics` | 예 |

실제 통합 실행 기록은 `docs/guide/`의 단계별 자료에 있다 (LOCAL-1~2, SWF-2~13).

## 21. 구현 점검표 (완료 정의)

### 21.1 이 명세를 따르는 구현에 필요한 것

- [x] 트래커 어댑터: 상태별 조회, ID 조회, 상태 이동, 팩토리 표시 댓글, 첨부, 이슈 생성, 팀 준비
- [x] 단일 실행 오케스트레이터: 폴링, 후보 정렬, 선점, 재시작 복구, Canceled 정리, 일시 정지
- [x] 작업 명세 변환: LLM 구조화, 지표 목표의 방향·인용·숫자 검증, 기본 수용 조건
- [x] 이슈별 작업 공간과 브랜치, 재사용, 작업 공간 자료, 커밋 제외 규칙
- [x] 에이전트 실행: 비대화식, 개인 설정 차단, 비용·시간 상한, 비밀값 제거, 이벤트 기록
- [x] 시도와 피드백: 실패 증거를 다음 시도에 넘기고, 시도를 다 쓰면 사람에게 넘긴다
- [x] 평가기: 결정적 정답 장면, 공개·홀드아웃 세트, 측정, 채점, 안정성, 계약 스모크, 프로세스 관찰
- [x] 수용 조건 판정과 같은 시드 회귀 비교
- [x] 리팩터링 단계: 코드 지표, 동작·충실도·구조 조건, 거부 시 되돌림
- [x] 병합과 배포, 배포 실패 시 되돌림
- [x] 평가 자산 경로: 범위 검사, 채점기 검증, 병합 뒤 다시 시작
- [x] 발견 루프: 커밋 기반 재평가, 잡음 반영 미달 판정, 단계적 목표, 중복·상한
- [x] 트래커 보고: 시작, 명세, 시도, 완료, Needs Human과 다음 행동

### 21.2 권장 확장 (아직 없음)

- [ ] 학습 루프: 실행 기록으로 하네스 후보를 만들고, 같은 작업으로 비교해 승격하거나 되돌린다
- [ ] 평가 자산 경로의 코드 지표 조건과 동작 보존 검사 (바꾸기 전 출력을 기준값으로 비교)
- [ ] 코드 지표가 기준을 넘는 항목을 모두 보여 주기
- [ ] 생성 시간 목표, 제품의 Codex 호출 상한
- [ ] 숫자로 적을 수 없는 요구사항을 수용 조건으로 바꾸는 방법
- [ ] 홀드아웃 기록과 비밀값 파일에 대한 에이전트 접근 차단 (샌드박스)
- [ ] 시도 도중의 조정 (실행 중 취소)
- [ ] 동시 실행과 병합 큐
- [ ] 단위 시험 묶음

## 부록 A. Symphony SPEC과의 대응

| Symphony SPEC | 이 명세 | 같은 점 | 다른 점 |
|---|---|---|---|
| §1 문제 정의 | 1절 | 트래커에서 일을 읽어 이슈별 작업 공간에서 에이전트를 실행한다 | 완료 판정이 eval이다. 트래커 쓰기는 에이전트가 아니라 팩토리가 한다 |
| §4 도메인 모델 | 4절 | 이슈 정규화, 작업 공간 키 규칙 | 작업 명세, 수용 조건, 판정, 평가 요약이 더 있다 |
| §5 `WORKFLOW.md` | 5절 | 정책을 저장소에 버전으로 둔다 | 파일 하나가 아니라 권한별로 나눴다 (실행 정책, 평가 정책, 하네스, 계약) |
| §7 상태 기계 | 7절 | 트래커 상태로 복구한다 | 메모리 선점 상태 없이 `In Progress`가 선점 기록이다. `Needs Human`이 있다 |
| §8 폴링·조정 | 8절 | 폴링, 후보 정렬, 조정, 종료 상태 정리 | 동시성 1, 백오프 대신 피드백 시도, 조정은 시도 전에만 |
| §9 작업 공간 | 9절 | 이슈별 결정적 작업 공간, 재사용 | git worktree와 브랜치, hook 없음, `.factory/` 자료 |
| §10 에이전트 실행 | 10절 | 비대화식 실행, 시간 상한, 이벤트 기록 | 시도마다 Claude Code 한 세션. app-server 연속 턴은 제품의 Codex에서만 쓴다 |
| §11 트래커 연동 | 11절 | 상태 목록 조회와 ID 조회, 오류 시 틱 건너뛰기 | 쓰기는 컨트롤러만 한다 |
| §12 프롬프트 | 5.2, 12절 | 저장소의 템플릿 | 이슈를 먼저 작업 명세로 바꾼다 |
| §13 관측 | 16절 | 구조화된 기록 | HTTP API 대신 Linear 댓글과 평가 시트 |
| §14 실패와 복구 | 17절 | DB 없는 재시작 복구 | 일시 정지 이슈 |
| §15 보안 | 18절 | 트래커 비밀값을 하위 프로세스에 넘기지 않는다 | 샌드박스 없음을 전제로 명시 |
| (없음) | 13~15절 | | 평가기, eval 관문, 리팩터링 단계, 평가 자산 경로, 발견 루프 |

## 부록 B. 알려진 한계

| 번호 | 한계 | 영향 |
|---|---|---|
| B1 | 평가 자산 경로의 수용 조건은 범위와 채점기 검증뿐이다. 코드 지표와 동작 보존을 확인하지 않는다 | 장면 생성이 조금 바뀌거나 복잡도가 늘어도 병합된다 (SWF-13에서 사람이 따로 확인) |
| B2 | 코드 지표는 지표마다 가장 나쁜 항목 하나만 보여 준다 | 고칠 때마다 다음 문제가 한 겹씩 드러난다 |
| B3 | 숫자가 없는 요구사항은 수용 조건이 되지 않는다 | 에이전트가 지키지 않아도 eval을 통과한다 |
| B4 | 에이전트는 `var/`의 평가 기록(홀드아웃 정답 포함)과 `.env`를 파일로 읽을 수 있다. 피드백의 평가 시트에도 홀드아웃 행이 있다 | 홀드아웃의 비공개성과 비밀값 격리는 지시문과 환경 변수에 기댄다 |
| B5 | 생성 시간 목표와 Codex 호출 상한이 없다 | 안정성을 높이는 동안 생성 시간이 약 50% 늘었지만 막지 않았다 |
| B6 | 안정성 최종 목표가 측정 잡음과 비슷한 크기다 | 같은 코드에서 목표 충족 여부가 평가마다 달라진다 (고정 시드 세 번 중 한 번 충족) |
| B7 | 하루 비용은 실행 기록의 Claude Code 비용만 센다 | 명세 변환, 발견 관찰, Codex 비용이 상한에 반영되지 않는다 |
| B8 | 동시성 1, 조정은 시도 전에만 | 긴 시도는 취소해도 끝까지 간다 |
| B9 | 평가 자산 작업 공간 키는 해시를 붙이지 않는다. Canceled 정리는 해시를 붙인 키로 찾는다 | 특수 문자가 있는 ID는 평가 자산 작업 공간이 정리되지 않을 수 있다 (Linear 식별자는 해당 없음) |
| B10 | 처리 중 트래커 쓰기 오류는 오케스트레이터를 끝낸다 | 다시 시작해야 한다 (복구는 자동) |
| B11 | 배포 확인은 상태 주소 응답만 본다 | 배포한 제품의 장면 생성 실패는 다음 평가에서야 드러난다 |
| B12 | 학습 루프가 없고 하네스는 `v1` 하나다 | 실행 기록에서 반복되는 실패 패턴이 지시문에 반영되지 않는다 |
