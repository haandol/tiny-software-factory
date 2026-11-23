# 앱 계약 (CONTRACT.md)

이 문서는 팩토리의 평가기가 제품(`app.py`)을 관찰하는 방법을 고정한다.
평가기는 `app.py`의 내부를 모른 채 아래 순서만 따른다. 이 계약은 팩토리가 소유한다.
제품 작업에서는 바꿀 수 없고, 사람이 등록한 `factory-eval` 이슈로만 바뀐다.

## 1. 실행

```sh
uv run app.py --port <port> --data-dir <dir>
```

- 평가기는 `app.py`가 있는 폴더에서 이 명령을 실행하고, 포트가 열릴 때까지 최대 180초 기다린다.
- 의존성이 필요하면 `app.py` 머리의 인라인 메타데이터(PEP 723)로 선언한다.
- 평가기는 환경 변수 `CODEX_HOME`(제품 전용 Codex 설정)과 `AWS_PROFILE=default`를 넘긴다. Codex를 부를 때는 이 환경을 그대로 쓴다.
- Codex는 `codex app-server`(표준 입출력 JSON-RPC)로 부르고, 앱과 같은 프로세스 그룹에서 실행한다. 모델은 `CODEX_HOME` 설정(`us.openai.gpt-6.1-sol`)을 따른다.
  평가기는 평가하는 동안 앱의 프로세스 그룹을 관찰하고, 작업 명세에 `process` 조건이 있으면 그 결과로 판정한다.

## 2. HTTP

| 요청 | 응답 |
|---|---|
| `POST /api/requests` (본문 = 이미지 바이트, `Content-Type: image/png` 또는 `image/jpeg`) | `202` `{"key": "<키>"}`. 장면 생성은 응답 뒤에 진행한다 |
| `GET /api/requests/<키>` | `200` `{"status": "pending" \| "done" \| "failed", "error": "<실패 이유, 선택>"}` |
| `GET /view?key=<키>` | `200` 뷰어 HTML |

- `done`은 장면 파일이 아래 형식으로 저장된 뒤에만 돌려준다.
- 평가기는 이미지 여러 장을 한꺼번에 요청한 뒤 상태를 2초마다 확인한다. 요청 하나당 최대 900초를 기다린다.

## 3. 장면 파일

- 위치: `<data-dir>/scenes/<키>.js`
- 형식: three.js를 인자로 받는 ES 모듈

```js
export function build(THREE) {
  // ...
  return { scene, camera };   // THREE.Scene, THREE.PerspectiveCamera
}
```

## 4. 뷰어

`/view?key=<키>`는 장면 파일을 불러와 그리고, 다음 객체를 노출한다.

```js
window.__PREVIZ__ = {
  ready,    // Promise. 첫 렌더링이 끝나면 resolve
  scene,    // THREE.Scene
  camera,   // THREE.PerspectiveCamera. ready 시점에 원본 사진과 같은 시점이어야 한다
};
```

- `window.__PREVIZ__`는 페이지가 열린 뒤 30초 안에 생겨야 하고, `ready`는 60초 안에 resolve되어야 한다.
- 평가기는 640×480 뷰포트로 연다.
- three.js는 로컬 사본을 쓴다. 평가기는 `localhost`·`127.0.0.1`이 아닌 주소로 가는 요청을 막고 계약 위반으로 기록한다.

## 5. 장면 규칙

- 좌표계는 y축이 위다. 길이 단위는 자유다. 평가기는 모든 위치와 크기를 카메라 높이(카메라와 바닥의 높이 차)로 나눠 비교한다.
- 메시마다 `mesh.userData.role`을 붙인다.

| role | 의미 | 개수 |
|---|---|---|
| `ground` | 바닥 | 1개 이상 (필수) |
| `wall` | 벽, 천장 등 공간 경계 | 0개 이상 |
| `object` | 주요 물체. 물체 하나당 메시 하나 | 0개 이상 |

- `role`이 없는 메시는 측정하지 않는다. 물체 하나를 여러 메시로 만들면 각각 다른 물체로 센다.
- 물체의 대표 색은 `mesh.material.color`로 읽는다.

## 6. 측정과 채점

평가기는 정답을 아는 합성 장면을 렌더링해 입력으로 쓰고, 뷰어의 장면 그래프를 정답과 비교한다.

| 항목 (지표 키) | 측정 방법 | 최종 목표 |
|---|---|---|
| 카메라 화각 (`camera.fov_error_deg`) | 수직 FOV 차이 | 5° 이하 |
| 카메라 기울기 (`camera.pitch_error_deg`, `camera.roll_error_deg`) | 피치·롤 각도 차이 | 각 3° 이하 |
| 물체 검출 (`objects.f1`) | 카메라 기준 좌표에서 가장 가까운 쌍끼리 짝지은 F1 (카메라 높이 1배 이내만 짝) | 0.8 이상 |
| 물체 배치 (`objects.position_error`) | 짝지은 물체의 바닥면 위치 오차 ÷ 방 대각선 | 0.10 이하 |
| 물체 크기 (`objects.size_error`) | 짝지은 물체의 크기 상대 오차 (가로·세로는 큰 값끼리 비교) | 0.20 이하 |
| 물체 색 (`objects.color_delta_e`) | 대표 색 차이 ΔE (CIE76) | 15 이하 |
| 재투영 일치 (`reprojection.iou`) | 같은 카메라로 다시 그린 물체 실루엣과 정답 실루엣의 IoU | 0.7 이상 |
| 뷰어 오류 (`viewer.console_errors`) | 콘솔 오류와 페이지 예외 수 | 0 |
| 생성 시간 (`run.seconds`) | 요청부터 `done`까지 | 측정 후 결정 |

충실도 점수(`fidelity`, 0~100)는 다음과 같다. 물체를 하나도 맞히지 못하면 카메라가 맞아도 0점이다. 프리비즈의 핵심이 물체 배치이기 때문이다.

```text
fidelity = 100 × F1 × (0.20·화각 + 0.15·기울기 + 0.20·배치 + 0.15·(크기·색) + 0.30·IoU)
  화각   = max(0, 1 − 화각 오차/30°)
  기울기 = max(0, 1 − (피치 오차 + 롤 오차)/2 / 15°)
  배치   = max(0, 1 − 위치 오차/0.5)
  크기·색 = (max(0, 1 − 크기 오차) + max(0, 1 − ΔE/60)) / 2
```

- 공개 세트: 고정 시드 4개. 이미지와 정답이 작업 공간에 공개된다.
- 홀드아웃: 평가할 때마다 새로 뽑는 시드. 그림자, 바닥·벽 질감 노이즈, 이미지 노이즈, 더 넓은 조명·롤 범위가 섞인다. 공개되지 않는다.
