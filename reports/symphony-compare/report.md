# 지금 팩토리와 OpenAI Symphony: 같은 골격, 다른 완료 기준

## 목표

이 리포트는 지금 팩토리와 OpenAI Symphony의 차이를 책임 단위로 구분하게 한다. 읽고 나면 두 구성이 어디까지 같고 어디서 갈라지는지 설명할 수 있다. 또 Symphony에서 바로 가져올 것과 이 실험이 일부러 다르게 둔 것을 나눌 수 있고, 팩토리의 실행기를 Codex app-server로 바꿀지 결정할 수 있다.

## 배경

사용자가 가리킨 리포트는 1차 아키텍처 계획이다. 그 계획은 Nuxt 4 제품, SQLite 상태 저장, Claude Code CLI 실행기를 전제로 했다. 이후 사용자 지시로 구성이 여러 번 바뀌었다. 팩토리는 Python \`factory.py\`와 \`tsf/\` 모듈이 되었고, 작업 상태의 원본은 Linear로 옮겨 데이터베이스를 뺐다. eval을 통과한 변경만 수용하고 실패는 Needs Human으로 사람에게 넘긴다. 병합 전 리팩터링 단계가 생겼고, 제품은 Codex를 \`codex app-server\` 방식과 GPT-6.1-Sol로 부르도록 바꾸는 중이다. 그래서 이 리포트는 링크된 1차 계획이 아니라 지금 실제로 구현된 구성을 기준으로 비교한다.

구현은 3단계까지 끝났다. 평가기, 단일 작업 실행기(\`run\`), 리팩터링 단계가 동작한다. Linear를 폴링해 이슈를 자동으로 처리하는 4단계는 설계만 있고 아직 구현하지 않았다. 아래에서 '계획'이라고 쓴 항목은 4단계 설계를 뜻한다.

Symphony는 이슈 트래커에서 일을 읽어 이슈마다 코딩 에이전트를 실행하는 서비스를 정의한 OpenAI의 오픈소스 명세다. 이 리포트는 2026-11-23에 받은 \`SPEC.md\`(Draft v1, 2,312줄) 원문을 읽고 비교했다. 우리 쪽 사실은 저장소 코드와 실행 기록에서 확인했다.

---

## 결론: 대기열과 작업 공간은 같고, 완료 판정과 실행 방식이 다르다

두 구성은 같은 골격을 쓴다. 이슈 트래커를 작업 대기열로 읽는 결정론적 오케스트레이터가 있고, 이슈마다 작업 공간을 하나 두고, 데이터베이스 없이 트래커 상태와 파일 시스템으로 재시작을 복구한다. 1차 계획의 SQLite를 빼고 Linear를 작업 상태의 원본으로 삼은 지금 구성은 Symphony와 같은 결론에 도달했다.

가장 큰 차이는 '언제 끝났다고 보는가'다. Symphony는 스케줄러이자 실행기에 머문다. 무엇이 완료인지와 티켓 상태 변경은 저장소의 \`WORKFLOW.md\` 프롬프트에 따라 에이전트가 정하고, 성공한 실행도 Human Review 같은 인계 상태에서 끝날 수 있다. 지금 팩토리는 제품 밖의 평가기가 수용 조건을 판정하고, 통과한 변경만 프로그램이 병합하며, Linear 상태와 댓글도 에이전트가 아니라 프로그램이 쓴다.

실행 방식도 다르다. Symphony의 실행기는 Codex app-server다. 이슈 하나에 세션 하나를 유지하고, 같은 스레드에서 턴을 이어 보낸다. 지금 팩토리의 실행기는 Claude Code이고, 시도마다 프로세스를 새로 띄워 실패 증거 파일로 다음 시도에 넘긴다. Codex app-server는 제품 안에서 장면을 측정하는 데만 쓴다.

Symphony에서 바로 가져올 것이 있다. 지금 팩토리는 Linear API 키를 에이전트와 제품 프로세스에 그대로 넘긴다. Symphony는 트래커 자격 증명을 에이전트 하위 프로세스에 넘기지 말라고 요구한다. Linear를 연결하는 4단계 전에 이 문제를 고쳐야 한다. 결정할 것도 하나 있다. 'GPT-6.1-Sol을 codex app-server 방식으로'라는 지시가 팩토리의 실행기를 뜻했다면, 실행기를 Symphony처럼 Codex app-server로 바꿔야 한다. 지금은 이 지시를 제품 쪽 Codex 호출로 해석해 작업 명세 LOCAL-2로 진행하고 있다.

<a id="overview"></a>

## 전체 그림

비교는 네 부분으로 나눈다. 첫째, 두 구성이 공유하는 골격이다. 트래커, 오케스트레이터, 작업 공간이 같은 자리에 있다. 둘째, 완료를 누가 판정하고 트래커에 누가 쓰는가다. 두 구성이 가장 크게 갈라지는 곳이다. 셋째, 에이전트를 어떻게 실행하고 그 정책을 어디에 두는가다. 넷째, 동시성·재시도·조정·관측·비밀값 같은 운영 장치다. 여기서 Symphony에서 가져올 것이 가장 많이 나온다.

아래 그림은 두 흐름을 나란히 놓은 것이다. 왼쪽 Symphony에서는 에이전트 세션이 트래커에 직접 쓰고 실행이 끝난다. 오른쪽 팩토리에서는 에이전트의 결과가 평가기를 거쳐야만 병합되고, 트래커에 쓰는 것은 컨트롤러다.

```mermaid
flowchart LR
  subgraph S["Symphony"]
    ST[(이슈 트래커)] -->|폴링 · 조정| SO[오케스트레이터]
    SO -->|이슈별 작업 공간| SR["Codex app-server 세션<br/>같은 스레드에서 턴 반복"]
    SR -->|provider-native 도구로 상태·댓글 쓰기| ST
    SR -->|정상 종료| SH["인계 상태<br/>예: Human Review"]
  end
  subgraph F["지금 팩토리"]
    FT[(Linear)] -->|"폴링 · 선점 (계획)"| FC[컨트롤러]
    FC -->|이슈별 git worktree| FA["Claude Code<br/>시도마다 새 실행"]
    FA -->|후보 커밋| FE["평가기<br/>정답 장면 · 수용 조건"]
    FE -->|실패, 시도 남음| FA
    FE -->|통과| FM["병합 · 배포<br/>(리팩터링 단계)"]
    FE -->|시도 소진| FN[Needs Human]
    FM -->|Done · 점수 댓글| FT
    FN -->|실패 요약 댓글| FT
  end
```

Symphony에서 트래커로 돌아가는 화살표는 에이전트 세션에서 나간다. 팩토리에서는 평가기의 판정을 거친 뒤 컨트롤러가 쓴다. 이 차이가 두 구성의 완료 기준 차이다.

1. [같은 골격: 트래커를 대기열로 쓰는 결정론적 오케스트레이터](#shared) — Symphony의 구성요소 여덟 개가 지금 팩토리의 어느 모듈에 대응하는지와, 상태를 데이터베이스 없이 다루는 공통 설계를 보여 준다.
2. [가장 큰 차이: 완료를 누가 판정하고, 트래커에 누가 쓰는가](#completion) — Symphony는 완료 판정을 워크플로 프롬프트와 에이전트에 맡기고, 지금 팩토리는 제품 밖 평가기와 프로그램에 둔다는 차이를 보여 준다.
3. [실행기와 정책: 한 세션을 이어 쓰는가, 시도마다 새로 띄우는가](#runner) — Symphony의 Codex app-server 세션 방식과 팩토리의 Claude Code 시도 방식, 그리고 정책을 제품 저장소에 둘지 팩토리 저장소에 둘지의 차이를 보여 준다.
4. [운영과 안전: 가져올 것과 일부러 다르게 둔 것](#operations) — Symphony의 운영 장치를 항목별로 맞대고, 4단계 전에 가져올 것과 이 실험이 일부러 다르게 둔 것을 나눈다.

<a id="shared"></a>

## 같은 골격: 트래커를 대기열로 쓰는 결정론적 오케스트레이터

Symphony의 구성요소 여덟 개가 지금 팩토리의 어느 모듈에 대응하는지와, 상태를 데이터베이스 없이 다루는 공통 설계를 보여 준다.

두 구성이 공유하는 구성요소와 그 대응. 완료 판정과 실행 방식의 차이는 다음 절에서 다룬다.

두 구성은 같은 문제를 같은 방식으로 푼다. 이슈 트래커에서 일을 읽어 오고, 이슈마다 격리된 작업 공간을 만들고, 그 안에서 코딩 에이전트를 실행한다. 작업 선택·선점·재시도는 LLM이 아니라 일반 코드가 한다.

상태를 다루는 방식도 같다. Symphony는 영속 데이터베이스 없이 트래커와 파일 시스템으로 재시작을 복구하고, 메모리에 있던 스케줄러 상태는 복원하지 않는다. 지금 팩토리도 1차 계획의 SQLite를 빼고, Linear의 In Progress 상태를 선점 기록으로 쓰며, 실행 기록은 파일로 남긴다. 처음에는 다른 길로 시작했지만 같은 결론에 도달했다.

**Symphony 구성요소와 지금 팩토리의 대응**

| Symphony 구성요소 (§3.1) | 지금 팩토리 | 상태 |
| --- | --- | --- |
| Workflow Loader, Config Layer | \`tsf/config.py\`(실행 정책), \`tsf/oracle/policy.py\`(평가 정책), \`harness/v1/\*.md\`(지시문) | 구현 |
| Issue Tracker Adapter | \`tsf/linear.py\` (GraphQL 클라이언트, 팀 준비) | 팀 준비만 구현, 폴링은 계획 |
| Orchestrator | \`tsf/worker.py\`의 실행 하나 (\`run\`) | 작업 명세 하나 실행만 구현, 상주 폴링(\`up\`)은 계획 |
| Workspace Manager | \`prepare\_worktree()\`: 작업 ID별 git worktree, 있으면 재사용 | 구현 |
| Agent Runner | \`run\_claude()\`: Claude Code 비대화식 실행 | 구현 |
| Status Surface (선택) | 평가 시트, \`docs/capture.py\` 이미지, 소개 자료 HTML | 실시간 상태 화면은 없음 |
| Logging | \`run.json\`, \`attempt-N.jsonl\`, 평가 \`summary.json\` | 구조화 로그 줄은 없음 |
| (없음) 평가 | \`tsf/oracle/\`: 정답 장면 평가기, 수용 조건, 코드 지표 | Symphony 범위 밖, 구현 |

표의 마지막 줄이 다음 절의 주제다. Symphony에는 평가 구성요소가 없다. Symphony의 명세는 스스로를 "스케줄러/실행기이자 트래커 읽기 도구"로 한정하고, 범용 워크플로 엔진이나 티켓 처리 로직을 목표에서 뺀다.

**같은 골격: 트래커를 대기열로 쓰는 결정론적 오케스트레이터 · 이해도 확인**

**Q4. 팩토리 프로세스가 작업 도중 죽었다가 다시 시작됐다. 두 구성이 공통으로 기대는 복구 근거는 무엇인가?**

선택지를 보기 전에 답을 떠올려 보세요.

A. 영속 데이터베이스에 저장된 스케줄러 상태

B. 트래커의 이슈 상태와 파일 시스템에 남은 작업 공간·실행 기록

C. 에이전트 세션의 대화 기록

D. 메모리에 있던 재시도 타이머

확인하기 전에 선택 이유를 한 문장으로 설명해 보세요. 입력하거나 채점하지 않습니다.

---

**정답과 해설 — 답을 고른 뒤 확인하세요**

**Q4: B** — 두 구성은 같은 골격을 쓴다. 상태의 원본은 트래커이고, 작업 공간과 실행 기록은 파일로 남는다. 재시작하면 이 둘을 다시 읽어 이어 간다.

근거: '같은 골격' 절의 둘째 문단과 E2

A. 두 구성 모두 영속 데이터베이스를 쓰지 않는다. 지금 팩토리는 1차 계획의 SQLite를 뺐다.

B. 두 구성 모두 트래커와 파일 시스템으로 다시 시작한다.

C. Symphony도 재시작 뒤 실행 중이던 세션을 복구할 수 있다고 가정하지 않는다.

D. Symphony 명세는 재시작 시 재시도 타이머를 복원하지 않는다고 명시한다.

이해를 돕기 위한 자가 확인입니다. 학습 효과를 측정하거나 보고서의 판정을 바꾸지 않습니다.

---

**같은 골격: 트래커를 대기열로 쓰는 결정론적 오케스트레이터 · 근거**

[Symphony SPEC.md §1 Problem Statement](<https://github.com/openai/symphony/blob/main/SPEC.md>)

```text
Symphony is a long-running automation service that continuously reads work from a configured issue tracker, creates an isolated workspace for each issue, and runs a coding agent session for that issue inside the workspace.
```

[Symphony SPEC.md §2.1 Goals: DB 없는 재시작 복구](<https://github.com/openai/symphony/blob/main/SPEC.md>)

```text
Support tracker/filesystem-driven restart recovery without requiring a persistent database; exact in-memory scheduler state is not restored.
```

<a id="completion"></a>

## 가장 큰 차이: 완료를 누가 판정하고, 트래커에 누가 쓰는가

Symphony는 완료 판정을 워크플로 프롬프트와 에이전트에 맡기고, 지금 팩토리는 제품 밖 평가기와 프로그램에 둔다는 차이를 보여 준다.

실행이 끝났다고 보는 조건, 병합 권한, 트래커 쓰기 권한. 에이전트를 실행하는 방식은 다음 절에서 다룬다.

Symphony에서 실행의 성공은 에이전트 세션이 정상적으로 끝나는 것이다. 무엇을 확인하고 언제 티켓을 넘길지는 \`WORKFLOW.md\`의 프롬프트에 적고, 에이전트가 그 지시에 따라 트래커 상태를 바꾼다. 성공한 실행도 \`Done\`이 아니라 \`Human Review\` 같은 인계 상태에서 끝날 수 있다. 결과가 맞는지 보는 일은 결국 사람이나 워크플로 밖의 CI에 남는다.

지금 팩토리에서 실행의 성공은 eval 통과다. 평가기는 정답을 아는 합성 장면으로 제품을 직접 실행하고, 작업 명세의 수용 조건을 하나씩 판정한다. 통과한 시도만 프로그램이 main에 병합하고 배포한다. 통과하지 못하면 병합하지 않고 Needs Human으로 넘긴다. 사람도 이 규칙을 우회하지 않고, 기준이 틀렸으면 \`factory-eval\` 이슈로 고친다. 사용자가 정한 "eval을 통과하지 못하면 수용하지 않는다"는 규칙을 따른 결과다.

**완료와 쓰기 권한의 비교**

| 항목 | Symphony | 지금 팩토리 |
| --- | --- | --- |
| 성공의 정의 | 에이전트 턴이 정상 종료하고, 이슈가 더 이상 활성 상태가 아님 | 작업 명세의 수용 조건을 모두 통과 (실행, 계약 스모크, 지표 목표, 홀드아웃 회귀, 프로세스) |
| 판정 주체 | 워크플로 프롬프트를 따르는 에이전트 | 제품 밖 평가기 (\`tsf/oracle/\`), 에이전트는 수정 불가 |
| 병합 | 범위 밖 (보통 에이전트가 PR을 만들고 사람이 검토) | 통과한 시도만 프로그램이 squash 병합 후 배포 |
| 트래커 쓰기 | 에이전트가 provider-native 도구로 쓰고, 도구는 호스트가 자격 증명으로 실행 | 컨트롤러가 상태 이동과 점수·실패 요약 댓글을 쓴다 (4단계 계획) |
| 실패의 끝 | 재시도 대기열에서 지수 백오프로 다시 실행 | 시도 3회 후 Needs Human, 사람이 새 이슈나 댓글로 다시 넣는다 |
| 기준 변경 | \`WORKFLOW.md\` 수정 (저장소에서 버전 관리) | \`factory-eval\` 이슈로만, 채점기 검증을 통과해야 수용 |

이 차이의 이유는 두 구성이 푸는 문제의 범위다. Symphony는 여러 팀이 각자의 워크플로를 올릴 수 있는 범용 실행기를 목표로 한다. 그래서 판정 로직을 일부러 비워 두고 워크플로에 맡긴다. 이 실험의 팩토리는 제품 하나와 Oracle 하나를 전제로, 사람 없이 결과를 판정하는 것이 목적이다. 판정을 에이전트에게 맡기면 에이전트가 기준을 낮추거나 스스로 완료를 선언하는 길이 열린다.

<a id="completion-tracker-writes"></a>

### 트래커에 쓰는 주체가 바뀌면 생기는 차이

에이전트가 쓰는 Symphony 방식과 프로그램이 쓰는 팩토리 방식에서, 상태 하나가 무엇을 보장하는지 비교한다.

트래커 쓰기 권한의 위치와 그 결과

Symphony에서는 이슈 상태가 에이전트의 판단을 나타낸다. 에이전트가 "다 했다"고 판단하면 상태를 옮긴다. 오케스트레이터는 그 상태를 읽어 실행을 멈출지 정할 뿐이다. 명세는 오케스트레이터에 상태·댓글 쓰기 API를 두지 않는 것을 중요한 경계로 꼽는다.

지금 팩토리에서는 이슈 상태가 평가 결과를 나타낸다. Done은 eval 통과, 병합, 배포, 배포본 재평가가 모두 끝났다는 뜻이고, Needs Human은 정해진 시도 안에 eval을 통과하지 못했다는 뜻이다. 에이전트는 트래커에 쓸 수단이 없다. 그래서 사람은 Linear만 보고도 결과를 믿을 수 있지만, 대신 팩토리는 상태 이동과 댓글 형식을 코드로 직접 관리해야 한다.

**가장 큰 차이: 완료를 누가 판정하고, 트래커에 누가 쓰는가 · 이해도 확인**

**Q1. 에이전트가 작업을 마쳤다고 판단했지만, 팩토리의 평가기가 홀드아웃 회귀로 실패를 판정했다. 지금 팩토리에서 Linear 이슈는 어떻게 되는가?**

선택지를 보기 전에 답을 떠올려 보세요.

A. 에이전트가 이슈를 Done으로 옮긴다.

B. 병합하지 않고, 시도가 남으면 실패 증거와 함께 다시 시도하며, 시도를 다 쓰면 컨트롤러가 Needs Human으로 옮긴다.

C. Human Review 상태로 옮겨 사람이 코드를 검토하게 한다.

D. 지수 백오프로 같은 시도를 몇 분 뒤 다시 실행한다.

확인하기 전에 선택 이유를 한 문장으로 설명해 보세요. 입력하거나 채점하지 않습니다.

나중에 이 보고서를 다시 열어 이 핵심 질문을 풀어보세요. 시간과 진행 상태는 저장하지 않습니다.

---

**정답과 해설 — 답을 고른 뒤 확인하세요**

**Q1: B** — 지금 팩토리에서 완료는 eval 통과이고, 판정은 제품 밖 평가기가, 트래커 쓰기는 컨트롤러가 한다. 통과하지 못하면 병합하지 않고 재시도한 뒤 Needs Human으로 넘긴다.

근거: '가장 큰 차이: 완료를 누가 판정하고, 트래커에 누가 쓰는가' 절의 비교표

A. 지금 팩토리의 에이전트에게는 트래커에 쓸 수단이 없다. Symphony 방식이라면 가능한 흐름이다.

B. 완료 판정은 평가기가 하고, 트래커 쓰기는 컨트롤러가 한다.

C. 인계 상태로 끝내는 것은 Symphony 워크플로의 예다. 지금 팩토리는 eval 실패를 Needs Human으로 넘긴다.

D. 백오프는 일시적 실패를 위한 Symphony의 재시도 방식이다. eval 실패는 같은 일을 다시 해서는 풀리지 않는다.

이해를 돕기 위한 자가 확인입니다. 학습 효과를 측정하거나 보고서의 판정을 바꾸지 않습니다.

---

**가장 큰 차이: 완료를 누가 판정하고, 트래커에 누가 쓰는가 · 근거**

[Symphony SPEC.md §1 Important boundary: 스케줄러·실행기, 트래커 쓰기는 에이전트](<https://github.com/openai/symphony/blob/main/SPEC.md>)

```text
Symphony is a scheduler/runner and tracker reader.
Ticket writes (state transitions, comments, PR links) are typically performed by the coding agent through provider-native tools executed by Symphony with the configured tracker credential.
A successful run can end at a workflow-defined handoff state (for example `Human Review`), not necessarily `Done`.
```

[지금 팩토리: 수용 조건 판정 (tsf/oracle/acceptance.py)](<../../tsf/oracle/acceptance.py>)

```text
"""수용 조건 판정: 작업 명세의 acceptance를 하나씩 판정한다. 모두 통과해야 eval 통과다."""
```

<a id="runner"></a>

## 실행기와 정책: 한 세션을 이어 쓰는가, 시도마다 새로 띄우는가

Symphony의 Codex app-server 세션 방식과 팩토리의 Claude Code 시도 방식, 그리고 정책을 제품 저장소에 둘지 팩토리 저장소에 둘지의 차이를 보여 준다.

에이전트 실행 프로토콜, 세션과 재시도, 시간 제한, 지시문과 정책 파일의 위치. 동시성과 비밀값은 다음 절에서 다룬다.

Symphony의 실행기는 Codex app-server에 고정되어 있다. 작업 공간에서 \`codex app-server\`를 띄우고, 표준 입출력 JSON-RPC로 세션을 열고, 첫 턴에 이슈 프롬프트를 보낸다. 턴이 끝났는데 이슈가 아직 활성 상태면, 같은 스레드에 이어서 턴을 보낸다. 이때 원래 프롬프트를 다시 보내지 않고 이어서 하라는 안내만 보낸다. 한 세션에서 최대 20턴까지 간다.

지금 팩토리의 실행기는 Claude Code다. 시도마다 \`claude -p\`를 새로 실행하고, 시도가 끝나면 프로세스도 끝난다. 다음 시도는 새 세션이므로 이전 대화를 기억하지 않는다. 대신 평가기가 남긴 실패 증거(\`.factory/feedback.json\`, 평가 시트, 앱 로그)와 에이전트의 노트를 작업 공간에 넣어 넘긴다. 이전 시도의 맥락은 대화 기록이 아니라 파일과 git 커밋으로 남는다.

**실행기와 정책의 비교**

| 항목 | Symphony | 지금 팩토리 |
| --- | --- | --- |
| 실행기 | Codex app-server (표준 입출력 JSON-RPC), 명령은 \`codex.command\`로 바꿀 수 있음 | Claude Code CLI \`claude -p --bare --output-format stream-json\` |
| 모델 | Codex 설정을 따름 | Bedrock default profile 기본값 (\`us.anthropic.claude-opus-5-5\`) |
| 세션 | 이슈 하나에 스레드 하나, 이어서 턴 (최대 \`max\_turns\` 20) | 시도마다 새 세션, 최대 3회 |
| 다음 턴·시도에 주는 것 | 이어서 하라는 안내 (원래 프롬프트는 스레드에 이미 있음) | 실패 증거 파일, 평가 시트, 노트, 이전 커밋 |
| 시간 제한 | 턴 무응답 1시간, 멈춤 감지 5분, 요청 응답 5초 | 시도당 전체 60분, 비용 $5 |
| 승인·샌드박스 | 구현마다 정하고 문서화 (예: 고신뢰 자동 승인) | \`--dangerously-skip-permissions\`, 같은 머신에서 격리 없음 |
| 지시문 | \`WORKFLOW.md\` 본문, 엄격한 템플릿(Liquid), 변수는 \`issue\`와 \`attempt\` | \`harness/v1/\*.md\`, Python \`str.format\`, 변수는 작업 명세와 시도 번호 |
| 정책 파일 위치 | 제품 저장소의 \`WORKFLOW.md\`, 코드와 함께 버전 관리, 바뀌면 재시작 없이 다시 읽음 | 팩토리 저장소 (\`harness/\`, \`tsf/oracle/policy.py\`), 제품 저장소와 분리, 다시 읽기 없음 |
| 작업 공간 준비 | 훅 4개 (\`after\_create\`, \`before\_run\`, \`after\_run\`, \`before\_remove\`) | 코드에 고정 (\`prepare\_worktree\`, \`write\_context\`) |

정책 파일 위치의 차이는 의도한 것이다. Symphony는 팀이 에이전트 지시를 코드와 함께 버전 관리하도록 제품 저장소에 둔다. 이 팩토리는 평가 기준과 지시문을 제품 저장소 밖에 둔다. 제품 저장소에서 일하는 에이전트가 자신의 평가 기준을 고칠 수 없게 하기 위해서다. 다만 '재시작 없이 다시 읽기'는 가져올 만하다. 지금은 지시문을 바꾸면 다음 실행부터만 반영된다.

<a id="runner-app-server"></a>

### app-server를 쓰는 위치가 다르다

Symphony는 팩토리의 실행기로, 지금 팩토리는 제품의 런타임으로 app-server를 쓴다는 차이와 그 결정 지점을 정리한다.

Codex app-server의 사용 위치와 진행 중인 LOCAL-2

두 구성 모두 \`codex app-server\`를 쓰지만 위치가 다르다. Symphony에서 app-server는 코드를 짜는 에이전트다. 지금 팩토리에서 app-server는 제품 안의 부품이다. 제품의 \`app.py\`가 이미지마다 Codex에게 장면 측정값을 묻는 데 쓴다. 코드를 짜는 에이전트는 Claude Code다.

사용자의 "GPT-6.1-Sol을 codex app-server 방식으로 써야 한다"는 지시는 이 중 제품 쪽으로 해석했다. 직전 대화에서 Codex를 쓰는 곳이 제품뿐이었기 때문이다. 이 해석에 따라 제품 전용 Codex 설정의 모델을 \`us.openai.gpt-6.1-sol\`로 고정했고, 제품의 호출 방식을 app-server로 바꾸는 작업 명세 LOCAL-2를 팩토리에 넣어 실행하고 있다. 지시가 팩토리의 실행기를 뜻했다면 Symphony처럼 \`run\_claude()\`를 app-server 클라이언트로 바꿔야 한다. 이 경우 이어서 턴 보내기, 토큰 집계, 무응답 감지를 함께 얻는다.

**팩토리 실행기 선택지**

| 선택지 | 얻는 것 | 잃거나 새로 생기는 것 |
| --- | --- | --- |
| Claude Code 유지 (지금) | 2·3단계 실측(첫 시도 통과, 리팩터링 수용)이 있다 | Symphony와 실행 방식이 다르다. 시도 사이에 대화 맥락이 끊긴다 |
| Codex app-server + GPT-6.1-Sol로 교체 | Symphony와 같은 실행 방식. 같은 스레드에서 이어서 턴, 턴 단위 토큰 집계, 무응답 감지 | 실행기·하네스 지시문을 다시 맞추고 2단계 실측을 다시 해야 한다. GPT-6.1-Sol의 코딩 품질은 아직 재지 않았다 |
| 둘 다 지원 (설정으로 선택) | 같은 작업을 두 실행기로 돌려 학습 루프에서 비교할 수 있다 | 코드와 하네스가 두 벌이 된다 |

**실행기와 정책: 한 세션을 이어 쓰는가, 시도마다 새로 띄우는가 · 이해도 확인**

**Q2. 두 번째 시도에서 에이전트가 첫 시도에서 왜 실패했는지 알아야 한다. 각 구성에서 그 정보는 어떻게 전달되는가?**

선택지를 보기 전에 답을 떠올려 보세요.

A. 두 구성 모두 같은 대화 스레드에 이어서 턴을 보내 전달한다.

B. Symphony는 같은 스레드의 이어지는 턴으로, 지금 팩토리는 작업 공간의 실패 증거 파일과 이전 커밋으로 전달한다.

C. 두 구성 모두 원래 프롬프트를 처음부터 다시 보낸다.

D. 지금 팩토리는 정보를 전달하지 않고 같은 프롬프트로 다시 시도한다.

확인하기 전에 선택 이유를 한 문장으로 설명해 보세요. 입력하거나 채점하지 않습니다.

---

**정답과 해설 — 답을 고른 뒤 확인하세요**

**Q2: B** — Symphony는 세션 하나를 유지하며 이어서 턴을 보내고, 지금 팩토리는 시도마다 새 세션을 띄우는 대신 실패 증거를 파일로 넘긴다.

근거: '실행기와 정책' 절의 첫 두 문단과 비교표

A. 지금 팩토리는 시도마다 Claude Code를 새로 실행하므로 대화 스레드가 이어지지 않는다.

B. Symphony는 스레드 기록에 맥락이 남고, 팩토리는 파일과 git에 맥락이 남는다.

C. Symphony 명세는 이어지는 턴에서 원래 프롬프트를 다시 보내지 말라고 한다.

D. 팩토리는 \`.factory/feedback.json\`, 평가 시트, 앱 로그, 노트를 다음 시도의 작업 공간에 넣는다.

이해를 돕기 위한 자가 확인입니다. 학습 효과를 측정하거나 보고서의 판정을 바꾸지 않습니다.

---

**실행기와 정책: 한 세션을 이어 쓰는가, 시도마다 새로 띄우는가 · 근거**

[Symphony SPEC.md §7.1: 같은 스레드에서 이어서 턴](<https://github.com/openai/symphony/blob/main/SPEC.md>)

```text
If the issue is still in an active state, the worker SHOULD start another turn on the same live coding-agent thread in the same workspace, up to `agent.max_turns`.
Continuation turns SHOULD send only continuation guidance to the existing thread, not resend the original task prompt that is already present in thread history.
```

[지금 팩토리: 시도마다 Claude Code를 새로 실행 (tsf/worker.py run\_claude)](<../../tsf/worker.py>)

```text
cmd = ["claude", "-p", prompt, "--bare", "--append-system-prompt", system,
       "--output-format", "stream-json", "--verbose",
       "--dangerously-skip-permissions", "--max-budget-usd", str(CLAUDE_BUDGET_PER_ATTEMPT_USD)]
```

<a id="operations"></a>

## 운영과 안전: 가져올 것과 일부러 다르게 둔 것

Symphony의 운영 장치를 항목별로 맞대고, 4단계 전에 가져올 것과 이 실험이 일부러 다르게 둔 것을 나눈다.

동시성, 재시도, 실행 중 조정, 작업 공간 정리, 비밀값, 관측, 적합성 시험. 각 항목을 가져올지 판단한다.

운영 장치에서는 Symphony가 훨씬 촘촘하다. Symphony는 여러 이슈를 동시에 돌리는 상주 서비스를 전제로 하고, 지금 팩토리는 작업 하나를 끝까지 실행하는 명령까지만 만들었기 때문이다. 4단계에서 상주 폴링을 만들 때 Symphony의 규칙을 대부분 그대로 가져올 수 있다.

**운영 장치 비교와 판단**

| 항목 | Symphony | 지금 팩토리 | 판단 |
| --- | --- | --- | --- |
| 비밀값 | 트래커 자격 증명을 에이전트 하위 프로세스에 넘기지 않는다 | \`agent\_env()\`가 \`os.environ\`을 복사해 \`LINEAR\_API\_KEY\`를 Claude Code·제품·Codex에 넘긴다 | 가져온다 (4단계 전 필수) |
| 실행 중 조정 | 틱마다 실행 중인 이슈 상태를 다시 읽어, 종료·비활성이면 실행을 멈춘다 | 없다. 사람이 Canceled로 옮겨도 진행 중인 시도는 끝까지 간다 | 가져온다 |
| 무응답 감지 | 마지막 이벤트 이후 5분이 지나면 멈춘 것으로 보고 다시 시도 | 시도당 전체 60분 제한만 있다 | 가져온다 (stream-json 이벤트 기준) |
| 작업 공간 이름 | \`\[A-Za-z0-9.\_-\]\` 외 문자는 \`\_\`로 바꾸고 해시를 붙인다 | 작업 ID를 그대로 경로에 쓴다 | 가져온다 |
| 구조화 로그 | 로그마다 \`issue\_id\`, \`issue\_identifier\`, \`session\_id\` | 실행 단위 JSON 파일, 콘솔 출력 | 가져온다 |
| 토큰 집계 | 세션별 입력·출력·전체 토큰과 누적값 | Claude Code 비용만 집계, Codex 토큰은 제품 로그에만 남음 | 가져온다 |
| 동시성 | 기본 10, 상태별 상한 | 1 (병합 충돌을 피하려고) | 일부러 다르게 둔다 |
| 재시도 | 정상 종료 후 1초 뒤 이어서, 실패는 10초·2ⁿ 백오프 (최대 5분) | 즉시 다음 시도, 3회 뒤 Needs Human | 일부러 다르게 둔다 (eval 실패는 일시적 오류가 아니다) |
| 작업 공간 정리 | 종료 상태 이슈의 작업 공간을 시작 시와 전환 시 지운다 | 병합 후 지우고, Needs Human이면 사람이 볼 수 있게 남긴다 | 계획대로 (Canceled 시 정리) |
| 적합성 시험 | §17 시험 행렬, §18 완료 점검표 | 채점기 검증(\`calibrate\`)만 있다 | 참고한다 |

재시도 방식을 다르게 두는 데는 이유가 있다. Symphony의 실패는 대부분 일시적이다. 네트워크, 시간 초과, 프로세스 종료 같은 실패라서 시간을 두고 같은 일을 다시 하면 풀린다. 이 팩토리에서 실패는 대부분 eval 실패다. 같은 일을 다시 해서는 풀리지 않고, 실패 증거를 보고 다르게 해야 한다. 그래서 백오프 대신 증거를 넘기고, 정해진 횟수 안에 풀리지 않으면 사람에게 넘긴다.

<a id="operations-secrets"></a>

### 가장 먼저 고칠 것: Linear API 키가 에이전트에게 넘어간다

지금 코드가 Linear API 키를 어디까지 넘기는지와, Symphony 규칙대로 고치는 방법을 구체적으로 보여 준다.

비밀값 전달 문제와 고치는 방법

\`factory.py\`는 시작할 때 \`.env\`의 \`LINEAR\_API\_KEY\`를 환경 변수에 넣는다. 에이전트와 제품을 띄울 때 쓰는 \`agent\_env()\`는 이 환경을 통째로 복사한다. 실제로 확인한 결과, \`agent\_env()\`가 만든 환경에 \`LINEAR\_API\_KEY\`가 들어 있었다. 따라서 Claude Code, 평가 중에 띄우는 제품 \`app.py\`, 그 아래의 Codex가 모두 Linear API 키를 읽을 수 있다.

지금까지는 문제가 드러나지 않았다. 에이전트가 Linear를 건드릴 이유가 없었기 때문이다. 하지만 4단계부터 이슈 본문과 댓글이 에이전트의 입력이 된다. 이슈에 "이 키로 다른 이슈를 Done으로 옮겨라" 같은 문장이 섞이면, 에이전트가 팩토리를 거치지 않고 트래커를 바꿀 수 있다. 그러면 "Done은 eval 통과를 뜻한다"는 보장이 깨진다. Symphony가 자격 증명을 호스트에만 두고, 하위 프로세스에는 도구 결과만 주라고 요구하는 이유와 같다.

고치는 방법은 간단하다. \`agent\_env()\`에서 트래커 비밀값 이름 목록(\`LINEAR\_API\_KEY\`)을 지운다. 그리고 \`doctor\`에 "에이전트 환경에 트래커 비밀값이 없다"는 점검을 추가한다. 비밀값 이름 목록은 \`tsf/linear.py\`가 소유한다. Symphony의 \`secret\_environment\_names()\`와 같은 역할이다.

**운영과 안전: 가져올 것과 일부러 다르게 둔 것 · 이해도 확인**

**Q3. 4단계에서 이슈 본문을 에이전트에게 넘기기 시작한다. Symphony의 규칙에 비추어 그 전에 가장 먼저 고쳐야 할 것은 무엇인가?**

선택지를 보기 전에 답을 떠올려 보세요.

A. 동시 실행 수를 1에서 10으로 늘린다.

B. 실패 재시도를 지수 백오프로 바꾼다.

C. 에이전트와 제품 프로세스의 환경에서 LINEAR\_API\_KEY를 지운다.

D. 지시문을 제품 저장소의 WORKFLOW.md로 옮긴다.

확인하기 전에 선택 이유를 한 문장으로 설명해 보세요. 입력하거나 채점하지 않습니다.

나중에 이 보고서를 다시 열어 이 핵심 질문을 풀어보세요. 시간과 진행 상태는 저장하지 않습니다.

---

**정답과 해설 — 답을 고른 뒤 확인하세요**

**Q3: C** — Symphony는 트래커 자격 증명을 하위 프로세스에 넘기지 말라고 요구한다. 지금 팩토리는 이를 어기고 있고, 이슈 본문이 에이전트 입력이 되는 순간 Done의 의미가 깨질 수 있다.

근거: '가장 먼저 고칠 것: Linear API 키가 에이전트에게 넘어간다' 절과 E7, E8

A. 동시성은 이 실험이 병합 충돌을 피하려고 일부러 1로 둔 항목이다.

B. eval 실패는 일시적 실패가 아니라서, 이 팩토리는 백오프 대신 증거를 넘긴다.

C. 지금은 키가 하위 프로세스에 넘어가, 이슈 본문에 섞인 지시로 에이전트가 트래커를 직접 바꿀 수 있다.

D. 지시문과 평가 기준은 에이전트가 고치지 못하게 일부러 팩토리 저장소에 둔다.

이해를 돕기 위한 자가 확인입니다. 학습 효과를 측정하거나 보고서의 판정을 바꾸지 않습니다.

---

**운영과 안전: 가져올 것과 일부러 다르게 둔 것 · 근거**

[Symphony SPEC.md §15.3 Secret Handling](<https://github.com/openai/symphony/blob/main/SPEC.md>)

```text
Do not pass tracker credentials through the coding-agent child environment. Adapters MUST declare secret environment names so local and remote launchers can remove them from child environments.
```

[지금 팩토리: 에이전트 환경이 전체 환경을 복사한다 (tsf/env.py agent\_env)](<../../tsf/env.py>)

```text
env = os.environ.copy()
env.update(AWS_PROFILE="default", AWS_REGION=region, AWS_DEFAULT_REGION=region,
           CLAUDE_CODE_USE_BEDROCK="1")

# 확인 결과 (2026-11-23): LINEAR_API_KEY in agent env: True
```

[Symphony SPEC.md §8.5 Active Run Reconciliation, §8.4 Retry and Backoff](<https://github.com/openai/symphony/blob/main/SPEC.md>)

```text
If tracker state is terminal: terminate worker and clean workspace.
…
Failure-driven retries use `delay = min(10000 * 2^(attempt - 1), agent.max_retry_backoff_ms)`.
```

내용 검토: Symphony SPEC.md 원문(2026-11-23, Draft v1)의 §1~11, §14~15, §18과 부록 A를 읽고 인용문을 원문과 대조했다. 지금 팩토리의 사실은 tsf/ 코드, 실행 기록, agent\_env() 실행 결과로 확인했다. 렌더러의 구조·다이어그램 문법 검사를 통과한 최종 출력을 다시 읽었다.

확인 한계: Symphony의 참조 구현 코드는 읽지 않았고 명세만 비교했다. §12~13(프롬프트 조립, 관측)과 §16~17(참조 알고리즘, 시험 행렬)은 훑어보기만 했다. 지금 팩토리의 4단계(Linear 폴링)는 설계만 있어, 해당 항목은 계획과 비교했다. 브라우저 화면 렌더링은 별도로 확인하지 않았다.
