#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26", "pillow>=10"]
# ///
"""tiny previz 진입점: 사진 한 장을 같은 구도의 three.js 프리비즈 장면으로 바꾸는 서비스를 띄운다 (구현은 previz/).

흐름
  POST /api/requests (이미지 바이트) → 키 발급(202) → 백그라운드 작업 (previz/worker.py)
    1) Codex를 헤드리스로 호출해(`codex exec -i <이미지> --output-schema`) 이미지 측정값을 JSON으로 받는다
       (화각, 지평선, 벽과 바닥의 경계선, 물체마다 bbox·바닥 모서리·대표 색, 주광 방향). (previz/codex.py, prompt.py)
    2) 측정값을 기하로 풀어 3D 장면을 만든다
       (지평선 → 피치·롤, 바닥 역투영 → 벽, 물체 모양을 투영해 bbox에 맞추는 최소제곱 피팅). (previz/geometry/)
    3) <data-dir>/scenes/<키>.js 에 `export function build(THREE)` ES 모듈로 저장한다. (previz/scene_file.py)
  GET /api/requests/<키>  → {"status": "pending" | "done" | "failed", "error"?}
  GET /view?key=<키>      → 로컬 three.js 사본으로 장면을 그리는 뷰어 (window.__PREVIZ__) (previz/static/viewer.html)

실행: uv run app.py --port <port> --data-dir <dir>
"""

import sys

from previz.cli import main

if __name__ == "__main__":
    sys.exit(main())
