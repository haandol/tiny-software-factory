"""전달: eval을 통과한 커밋을 main에 squash 병합하고, main을 로컬 서버로 다시 띄운다."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from .config import PRODUCT, PROD_PORT, VAR
from .env import git
from .oracle import http, start_product_app

PID_FILE = VAR / "prod.pid"


def stop_prod() -> None:
    if not PID_FILE.exists():
        return
    try:
        os.killpg(int(PID_FILE.read_text()), signal.SIGTERM)
        time.sleep(1)
    except (ProcessLookupError, ValueError, PermissionError):
        pass
    PID_FILE.unlink()


def deploy(allow_missing: bool = False) -> None:
    """product/ main의 app.py를 PROD_PORT에서 다시 띄운다. 이 서버가 이 실험의 프로덕션이다."""
    stop_prod()
    if not (PRODUCT / "app.py").exists():
        if allow_missing:
            return
        raise RuntimeError("product/app.py가 없다")
    app = start_product_app(PRODUCT, VAR / "prod-data", VAR / "prod.log", port=PROD_PORT)
    PID_FILE.write_text(str(app.pid))
    code, _ = http("GET", f"{app.base_url}/api/requests/not-a-key")
    if code not in (200, 404):
        raise RuntimeError(f"배포한 app.py가 상태 주소에 {code}로 응답했다")


def merge_and_deploy(item: dict, wt: Path, branch: str, commit: str) -> dict:
    """commit을 main에 squash 병합하고 배포한다. 배포가 실패하면 main을 되돌린다."""
    git("checkout", "-q", branch, cwd=wt)
    git("reset", "-q", "--hard", commit, cwd=wt)
    previous = git("rev-parse", "--short", "main", cwd=PRODUCT)
    git("merge", "-q", "--squash", branch, cwd=PRODUCT)
    goal = " ".join(str(item["goal"]).split())[:72]
    git("commit", "-q", "-m", f"{item['work_item']}: {goal}\n\neval 통과 (factory run, 후보 {commit})",
        cwd=PRODUCT, identity=True)
    merged = git("rev-parse", "--short", "HEAD", cwd=PRODUCT)
    try:
        deploy()
    except RuntimeError as e:
        git("reset", "-q", "--hard", previous, cwd=PRODUCT)
        deploy(allow_missing=True)
        return {"merged": None, "deployed": False, "error": str(e), "previous": previous}
    git("worktree", "remove", "--force", str(wt), cwd=PRODUCT)
    return {"merged": merged, "deployed": True, "previous": previous, "url": f"http://127.0.0.1:{PROD_PORT}"}
