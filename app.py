#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26", "pillow>=10"]
# ///
"""tiny previz 진입점: 사진 한 장을 같은 구도의 three.js 프리비즈 장면으로 바꾸는 서비스를 띄운다 (구현은 previz/).

흐름
  POST /api/requests (이미지 바이트) → 키 발급(202) → 백그라운드 작업 (previz/worker.py)
    1) 앱이 시작할 때 띄운 `codex app-server` 하나에 표준 입출력 JSON-RPC로 thread/start → turn/start
       (이미지 localImage + 지시문, outputSchema)를 보내 이미지 측정값을 JSON으로 받는다
       (화각, 지평선, 벽과 바닥의 경계선, 물체마다 bbox·바닥 모서리·대표 색, 주광 방향). (previz/codex.py, prompt.py)
       측정은 잴 때마다 흔들리므로 요청마다 표본 여러 개(PREVIZ_CODEX_SAMPLES, 기본 5)를 스레드마다 turn 하나씩 동시에 잰다.
    2) 표본마다 측정값을 기하로 풀어 3D 장면을 만들고 (previz/geometry/)
       (지평선 → 피치·롤, 바닥 역투영 → 벽, 물체 모양을 투영해 bbox에 맞추는 최소제곱 피팅),
       카메라 보정 잔차가 가장 작은 표본의 장면을 쓴다. (previz/consensus.py)
    3) <data-dir>/scenes/<키>.js 에 `export function build(THREE)` ES 모듈로 저장한다. (previz/scene_file.py)
  GET /api/requests/<키>  → {"status": "pending" | "done" | "failed", "error"?}
  GET /view?key=<키>      → 로컬 three.js 사본으로 장면을 그리는 뷰어 (window.__PREVIZ__) (previz/static/viewer.html)

실행: uv run app.py --port <port> --data-dir <dir>
"""

import sys

from previz.cli import main

if __name__ == "__main__":
    sys.exit(main())
