"""경로와 실행 정책. 이 값들은 사람이 정하고, 어느 루프도 스스로 바꾸지 않는다."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR = ROOT / "var"                       # 실행 중에 생기는 모든 것 (git 제외)
CODEX_HOME = VAR / "codex-home"          # 제품이 Codex를 부를 때 쓰는 전용 설정
PRODUCT = ROOT / "product"               # 제품 저장소 (별도 git, 팩토리만 커밋)
VENDOR = ROOT / "vendor"                 # 합성 장면 렌더링용 three.js 사본
HARNESS = ROOT / "harness"               # 에이전트 지시문 (버전별 폴더)
WORKTREES = VAR / "worktrees"
RUNS = VAR / "runs"
EVALS = VAR / "evals"

THREE_VERSION = "0.187.0"
THREE_FILES = ["build/three.module.js", "build/three.core.js", "LICENSE"]

# 생산 루프 정책
HARNESS_VERSION = "v1"
MAX_ATTEMPTS = 3
CLAUDE_BUDGET_PER_ATTEMPT_USD = 5
CLAUDE_TIMEOUT_S = 60 * 60
PROD_PORT = int(os.environ.get("FACTORY_PROD_PORT", "8000"))

# 팩토리가 만드는 커밋의 작성자
FACTORY_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "tiny-software-factory",
    "GIT_AUTHOR_EMAIL": "factory@localhost",
    "GIT_COMMITTER_NAME": "tiny-software-factory",
    "GIT_COMMITTER_EMAIL": "factory@localhost",
}
