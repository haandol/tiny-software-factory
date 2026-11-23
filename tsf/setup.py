"""0단계: 로컬 준비(init)와 환경 점검(doctor)."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

from .config import CODEX_HOME, CODEX_MODEL, FACTORY_GIT_IDENTITY, PRODUCT, ROOT, RUNS, THREE_FILES, THREE_VERSION, VAR, VENDOR, WORKTREES
from .env import agent_env, aws_default_region
from .linear import check_team
from .oracle.policy import CHROMIUM_ARGS


# --- init: 여러 번 실행해도 같은 결과 ------------------------------------------------

def write_toml(data: dict, prefix: str = "") -> str:
    """config.toml에 필요한 만큼만 지원하는 작은 TOML 작성기 (문자열, 숫자, 불리언, 표)."""
    scalars, tables = [], []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        elif isinstance(v, bool):
            scalars.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            scalars.append(f"{k} = {v}")
        else:
            scalars.append(f"{k} = {json.dumps(str(v), ensure_ascii=False)}")
    out = ""
    if scalars:
        if prefix:
            out += f"[{prefix}]\n"
        out += "\n".join(scalars) + "\n\n"
    for k, v in tables:
        out += write_toml(v, f"{prefix}.{k}" if prefix else k)
    return out


DISABLED_CODEX_FEATURES = ["plugins", "remote_plugin", "plugin_sharing", "apps", "browser_use", "hooks"]


def init_codex_home() -> Path:
    """사용자 Codex 설정에서 Bedrock 공급자와 모델만 옮긴 전용 설정을 만든다.

    MCP 서버, 플러그인, hook, 전역 AGENTS.md는 옮기지 않는다. 제품 동작이
    개인 설정에 따라 달라지지 않게 하기 위해서다.
    """
    user_config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text())
    keep = {k: user_config[k] for k in ("model_provider", "model", "model_catalog_json", "model_context_window")
            if k in user_config}
    providers = {name: conf for name, conf in user_config.get("model_providers", {}).items()
                 if name.startswith("amazon-bedrock")}
    for conf in providers.values():               # 어떤 공급자든 default profile을 쓴다
        if isinstance(conf.get("aws"), dict):
            conf["aws"]["profile"] = "default"
    if providers:
        keep["model_providers"] = providers
    keep["model"] = CODEX_MODEL                   # 제품 런타임 모델은 팩토리가 정한다
    # 제품 런타임에 필요 없는 기능은 끈다. 켜 두면 Codex가 실행마다 GitHub에서 플러그인 목록을 받아 온다
    # (3단계 평가 중 프로세스 관찰로 발견: git ls-remote https://github.com/openai/plugins.git).
    keep["features"] = {name: False for name in DISABLED_CODEX_FEATURES}
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    header = "# factory.py init이 만든 제품 전용 Codex 설정. 직접 고치지 않는다.\n\n"
    (CODEX_HOME / "config.toml").write_text(header + write_toml(keep))
    return CODEX_HOME / "config.toml"


def init_vendor_three() -> None:
    if all((VENDOR / Path(f).name).exists() for f in THREE_FILES):
        return
    url = f"https://registry.npmjs.org/three/-/three-{THREE_VERSION}.tgz"
    with urllib.request.urlopen(url, timeout=120) as resp:
        archive = tarfile.open(fileobj=io.BytesIO(resp.read()), mode="r:gz")
    VENDOR.mkdir(exist_ok=True)
    for name in THREE_FILES:
        member = archive.getmember(f"package/{name}")
        (VENDOR / Path(name).name).write_bytes(archive.extractfile(member).read())
    (VENDOR / "VERSION").write_text(f"three {THREE_VERSION}\n")


def init_product_repo() -> None:
    if (PRODUCT / ".git").exists():
        return
    PRODUCT.mkdir(exist_ok=True)
    env = {**os.environ, **FACTORY_GIT_IDENTITY}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=PRODUCT, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init: empty product repository"],
                   cwd=PRODUCT, env=env, check=True)


def init() -> None:
    for d in (VAR, RUNS, WORKTREES):
        d.mkdir(parents=True, exist_ok=True)
    print(f"codex-home   {init_codex_home().relative_to(ROOT)}")
    init_vendor_three()
    print(f"three.js     vendor/ ({THREE_VERSION})")
    init_product_repo()
    print(f"product      {PRODUCT.relative_to(ROOT)}/ (git main)")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    print("chromium     playwright 브라우저 설치 완료")


# --- doctor: 이후 단계가 실패할 원인을 미리 찾는다 ------------------------------------

def record_doctor(name: str, value: dict) -> None:
    path = VAR / "doctor.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[name] = value
    VAR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def check_tools() -> str:
    for tool in ("git", "uv", "claude", "codex", "aws"):
        if not shutil.which(tool):
            raise RuntimeError(f"{tool} 없음")
    return "git, uv, claude, codex, aws"


def check_local() -> str:
    missing = [str(p.relative_to(ROOT)) for p in
               (CODEX_HOME / "config.toml", VENDOR / "three.module.js", PRODUCT / ".git") if not p.exists()]
    if missing:
        raise RuntimeError(f"없음: {', '.join(missing)} (먼저 init)")
    return "codex-home, vendor/three.js, product 저장소"


def check_aws() -> str:
    out = subprocess.run(["aws", "sts", "get-caller-identity", "--profile", "default", "--query", "Arn"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    return f"default profile 자격 증명 정상 (region {aws_default_region()})"


def check_browser() -> str:
    """합성 장면 렌더링에 필요한 헤드리스 Chromium과 WebGL을 확인한다."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=CHROMIUM_ARGS)
        webgl = browser.new_page().evaluate("!!document.createElement('canvas').getContext('webgl2')")
        version = browser.version
        browser.close()
    if not webgl:
        raise RuntimeError("WebGL2 컨텍스트를 만들 수 없다")
    return f"chromium {version}, WebGL2 사용 가능"


def check_claude() -> str:
    """Claude Code를 비대화식으로 한 번 부른다. --bare로 개인 설정을 끊는다."""
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            ["claude", "-p", "Reply with exactly the word PONG.", "--bare",
             "--output-format", "stream-json", "--verbose", "--max-budget-usd", "0.5"],
            cwd=tmp, env=agent_env(), capture_output=True, text=True, timeout=300)
    events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
    init_ev = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in events if e.get("type") == "result"), {})
    if out.returncode != 0 or "PONG" not in str(result.get("result", "")):
        detail = result.get("result") or out.stderr.strip() or out.stdout.strip()
        raise RuntimeError(f"응답 이상: {str(detail)[:300]}")
    record_doctor("claude", {"model": init_ev.get("model"), "cost_usd": result.get("total_cost_usd")})
    return f"model={init_ev.get('model')} cost=${result.get('total_cost_usd', 0):.4f}"


def check_codex() -> str:
    """제품과 같은 방식(전용 CODEX_HOME, codex app-server JSON-RPC)으로 Codex를 한 번 부른다."""
    from .codex import AppServer
    if not (CODEX_HOME / "config.toml").exists():
        raise RuntimeError("var/codex-home이 없다 (먼저 init)")
    with tempfile.TemporaryDirectory() as tmp, AppServer(agent_env(CODEX_HOME=str(CODEX_HOME))) as server:
        thread = server.request("thread/start", {"cwd": tmp, "ephemeral": True})
        answer, usage, seconds = server.run_turn(thread["thread"]["id"],
                                                 [{"type": "text", "text": "Reply with exactly the word PONG."}])
    if "PONG" not in answer:
        raise RuntimeError(f"응답 이상: {answer[:200]}")
    model = thread.get("model") or tomllib.loads((CODEX_HOME / "config.toml").read_text()).get("model")
    record_doctor("codex", {"mode": "app-server", "model": model, "seconds": seconds, "usage": usage})
    return f"app-server, model={model}, {seconds}초, 입력 토큰 {usage.get('inputTokens') if usage else '?'}"


def check_secrets() -> str:
    """에이전트·제품에 넘기는 환경에 트래커 비밀값이 없는지 확인한다."""
    from .linear import SECRET_ENV_NAMES
    leaked = [n for n in SECRET_ENV_NAMES if n in agent_env()]
    if leaked:
        raise RuntimeError(f"하위 프로세스 환경에 비밀값이 남아 있다: {', '.join(leaked)}")
    return f"하위 프로세스 환경에서 {', '.join(SECRET_ENV_NAMES)} 제거됨"


def doctor(skip_agents: bool = False) -> bool:
    checks = [("도구", check_tools), ("로컬 준비", check_local), ("AWS", check_aws), ("비밀값 격리", check_secrets),
              ("Chromium", check_browser), ("Linear", check_team),
              ("Claude Code", check_claude), ("Codex", check_codex)]
    if skip_agents:
        checks = [c for c in checks if c[0] not in ("Claude Code", "Codex")]
    failed = 0
    for name, check in checks:
        try:
            print(f"OK    {name:<12} {check()}", flush=True)
        except Exception as e:  # 점검은 하나가 실패해도 나머지를 계속한다
            failed += 1
            print(f"FAIL  {name:<12} {e}", flush=True)
    print(f"\n{len(checks) - failed}/{len(checks)} 통과")
    return failed == 0
