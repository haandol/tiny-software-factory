"""실행 환경: .env, Bedrock default profile, git."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import FACTORY_GIT_IDENTITY, ROOT


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE 형식의 .env를 읽어 환경 변수에 넣는다. 이미 있는 값은 덮어쓰지 않는다."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def aws_default_region() -> str:
    out = subprocess.run(["aws", "configure", "get", "region", "--profile", "default"],
                         capture_output=True, text=True)
    return out.stdout.strip() or "us-east-1"


def agent_env(**extra: str) -> dict[str, str]:
    """에이전트 CLI에 넘길 환경. 셸의 AWS 설정과 무관하게 Bedrock default profile을 쓴다."""
    region = aws_default_region()
    env = os.environ.copy()
    env.update(AWS_PROFILE="default", AWS_REGION=region, AWS_DEFAULT_REGION=region,
               CLAUDE_CODE_USE_BEDROCK="1")
    env.pop("ANTHROPIC_MODEL", None)      # 모델은 지정하지 않는다
    from .linear import SECRET_ENV_NAMES
    for name in SECRET_ENV_NAMES:          # 트래커 비밀값은 하위 프로세스에 넘기지 않는다
        env.pop(name, None)
    env.update(extra)
    return env


def git(*args: str, cwd: Path, check: bool = True, identity: bool = False) -> str:
    env = {**os.environ, **FACTORY_GIT_IDENTITY} if identity else None
    out = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {out.stderr.strip()}")
    return out.stdout.strip()
