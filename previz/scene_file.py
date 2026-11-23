"""장면 파일: 장면 명세를 `export const SPEC` + `export function build(THREE)` ES 모듈 텍스트로 만든다."""

from __future__ import annotations

import json

from .config import static_text

SCENE_BUILDER_JS = static_text("scene_builder.js")     # SPEC으로 three.js 장면을 짓는 build 함수


def scene_module(spec: dict, key: str) -> str:
    return (f"// tiny previz 장면 {key}. app.py가 Codex 이미지 분석 결과로 만들었다.\n"
            f"// 형식: export function build(THREE) -> {{ scene, camera }}\n"
            f"export const SPEC = {json.dumps(spec, ensure_ascii=False, indent=1)};\n"
            f"\n{SCENE_BUILDER_JS}")
