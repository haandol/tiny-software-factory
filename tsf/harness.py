"""하네스: 에이전트에게 주는 지시문. harness/<버전>/<이름>.md를 읽어 값을 채운다.

학습 루프(6단계)는 이 폴더에 새 버전을 만든다. 실행 기록은 자신이 쓴 버전을 남긴다.
"""

from __future__ import annotations

from .config import HARNESS, HARNESS_VERSION


def render(name: str, version: str = HARNESS_VERSION, **values) -> str:
    text = (HARNESS / version / f"{name}.md").read_text()
    return text.format(**values) if values else text
