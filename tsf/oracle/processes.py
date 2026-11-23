"""앱이 실행한 하위 프로세스 관찰. 평가하는 동안 앱의 프로세스 그룹을 훑어 명령줄을 모은다.

"앱이 codex app-server를 쓰고 codex exec는 쓰지 않는다" 같은 런타임 요구사항을 수용 조건으로 확인하는 데 쓴다.
"""

from __future__ import annotations

import os
from pathlib import Path


def process_group_commands(pgid: int) -> set[str]:
    """프로세스 그룹 pgid에 속한 프로세스의 명령줄 (실행 파일은 이름만, 인자는 앞 4개까지)."""
    found = set()
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) != pgid:          # 괄호 뒤 세 번째 필드가 프로세스 그룹
                continue
            argv = (stat.parent / "cmdline").read_bytes().split(b"\0")
        except (OSError, ValueError, IndexError):
            continue
        words = [a.decode(errors="replace") for a in argv if a][:5]
        if words:
            found.add(" ".join([os.path.basename(words[0])] + words[1:]))
    return found


def matches(commands: set[str] | list[str], pattern: str) -> bool:
    """pattern의 첫 단어로 시작하는 실행 파일 뒤에 나머지 단어가 차례로 나오는 명령이 있는가.

    예: "codex app-server"는 "codex app-server -c x"와 "node codex.js app-server"에 모두 맞는다.
    """
    head, *rest = pattern.split()
    for cmd in commands:
        words = cmd.split()
        for i, w in enumerate(words):
            if os.path.basename(w).startswith(head) and words[i + 1:i + 1 + len(rest)] == rest:
                return True
    return False
