#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["playwright==1.65.2"]
# ///
"""tiny-software-factory — 가장 작은 소프트웨어 팩토리.

이 파일 하나가 팩토리 전체다. 단계마다 명령이 하나씩 늘어난다.

  0단계  init, doctor, linear-setup   로컬 준비, 환경 점검, Linear 준비
  1단계  eval, calibrate              (예정) 정답 장면 평가기와 채점기 검증
  2단계  run                          (예정) 로컬 작업 명세 하나를 끝까지 실행
  3단계  up                           (예정) Linear를 제어판으로 상주 실행

실행: uv run factory.py <명령>
"""

from __future__ import annotations

import argparse
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

# ---------------------------------------------------------------------------
# 경로와 상수
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
VAR = ROOT / "var"                       # 실행 중에 생기는 모든 것 (git 제외)
CODEX_HOME = VAR / "codex-home"          # 제품이 Codex를 부를 때 쓰는 전용 설정
PRODUCT = ROOT / "product"               # 제품 저장소 (별도 git, 팩토리만 커밋)
VENDOR = ROOT / "vendor"                 # 합성 장면 렌더링용 three.js 사본

THREE_VERSION = "0.187.0"
THREE_FILES = ["build/three.module.js", "build/three.core.js", "LICENSE"]

LINEAR_URL = "https://api.linear.app/graphql"
LINEAR_LABELS = {                        # 이름: (색, 설명)
    "factory": ("#5e6ad2", "팩토리가 제품 저장소에서 처리하는 이슈"),
    "factory-eval": ("#f2c94c", "팩토리가 평가 자산(oracle.py, CONTRACT.md)에서 처리하는 이슈"),
    "auto-discovered": ("#26b5ce", "발견 루프가 만든 이슈"),
}
NEEDS_HUMAN = "Needs Human"

# 팩토리가 만드는 커밋의 작성자
FACTORY_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "tiny-software-factory",
    "GIT_AUTHOR_EMAIL": "factory@localhost",
    "GIT_COMMITTER_NAME": "tiny-software-factory",
    "GIT_COMMITTER_EMAIL": "factory@localhost",
}


# ---------------------------------------------------------------------------
# 환경: .env와 Bedrock default profile
# ---------------------------------------------------------------------------

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
    out = subprocess.run(
        ["aws", "configure", "get", "region", "--profile", "default"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() or "us-east-1"


def agent_env(**extra: str) -> dict[str, str]:
    """에이전트 CLI에 넘길 환경. 셸의 AWS 설정과 무관하게 Bedrock default profile을 쓴다."""
    region = aws_default_region()
    env = os.environ.copy()
    env.update(
        AWS_PROFILE="default",
        AWS_REGION=region,
        AWS_DEFAULT_REGION=region,
        CLAUDE_CODE_USE_BEDROCK="1",
    )
    env.pop("ANTHROPIC_MODEL", None)      # 모델은 지정하지 않는다
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Linear: 팩토리의 제어판
# ---------------------------------------------------------------------------

def linear(query: str, variables: dict | None = None) -> dict:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RuntimeError("LINEAR_API_KEY가 없다 (.env 확인)")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        LINEAR_URL, data=body,
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if data.get("errors"):
        raise RuntimeError(f"Linear 오류: {data['errors']}")
    return data["data"]


def linear_team(team_key: str) -> dict:
    data = linear(
        """query($key: String!) {
          teams(filter: { key: { eq: $key } }) { nodes {
            id key name
            states { nodes { id name type } }
            labels { nodes { id name } }
          } }
        }""",
        {"key": team_key},
    )
    nodes = data["teams"]["nodes"]
    if not nodes:
        raise RuntimeError(f"Linear 팀 {team_key}를 찾을 수 없다")
    return nodes[0]


def team_key() -> str:
    return os.environ.get("FACTORY_LINEAR_TEAM", "SWF")


# ---------------------------------------------------------------------------
# init: 로컬 준비 (여러 번 실행해도 같은 결과)
# ---------------------------------------------------------------------------

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


def init_codex_home() -> Path:
    """사용자 Codex 설정에서 Bedrock 공급자와 모델만 옮긴 전용 설정을 만든다.

    MCP 서버, 플러그인, hook, 전역 AGENTS.md는 옮기지 않는다. 제품 동작이
    개인 설정에 따라 달라지지 않게 하기 위해서다.
    """
    user_config = tomllib.loads((Path.home() / ".codex" / "config.toml").read_text())
    keep = {k: user_config[k] for k in
            ("model_provider", "model", "model_catalog_json", "model_context_window")
            if k in user_config}
    providers = {
        name: conf for name, conf in user_config.get("model_providers", {}).items()
        if name.startswith("amazon-bedrock")
    }
    for conf in providers.values():               # 어떤 공급자든 default profile을 쓴다
        if isinstance(conf.get("aws"), dict):
            conf["aws"]["profile"] = "default"
    if providers:
        keep["model_providers"] = providers
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
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init: empty product repository"],
        cwd=PRODUCT, env=env, check=True,
    )


def init_browser() -> None:
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


def cmd_init(args: argparse.Namespace) -> int:
    for d in (VAR, VAR / "runs", VAR / "worktrees"):
        d.mkdir(parents=True, exist_ok=True)
    print(f"codex-home   {init_codex_home().relative_to(ROOT)}")
    init_vendor_three()
    print(f"three.js     vendor/ ({THREE_VERSION})")
    init_product_repo()
    print(f"product      {PRODUCT.relative_to(ROOT)}/ (git main)")
    init_browser()
    print("chromium     playwright 브라우저 설치 완료")
    return 0


# ---------------------------------------------------------------------------
# doctor: 이후 단계가 실패할 원인을 미리 찾는다
# ---------------------------------------------------------------------------

def check_tools() -> str:
    found = []
    for tool in ("git", "uv", "claude", "codex", "aws"):
        path = shutil.which(tool)
        if not path:
            raise RuntimeError(f"{tool} 없음")
        found.append(tool)
    return ", ".join(found)


def check_aws() -> str:
    out = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--profile", "default", "--query", "Arn"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200])
    return f"default profile 자격 증명 정상 (region {aws_default_region()})"


def check_claude() -> str:
    """Claude Code를 비대화식으로 한 번 부른다. --bare로 개인 설정을 끊는다."""
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            ["claude", "-p", "Reply with exactly the word PONG.", "--bare",
             "--output-format", "stream-json", "--verbose", "--max-budget-usd", "0.5"],
            cwd=tmp, env=agent_env(), capture_output=True, text=True, timeout=300,
        )
    events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    result = next((e for e in events if e.get("type") == "result"), {})
    if out.returncode != 0 or "PONG" not in str(result.get("result", "")):
        detail = result.get("result") or out.stderr.strip() or out.stdout.strip()
        raise RuntimeError(f"응답 이상: {str(detail)[:300]}")
    record_doctor("claude", {"model": init.get("model"), "cost_usd": result.get("total_cost_usd")})
    return f"model={init.get('model')} cost=${result.get('total_cost_usd', 0):.4f}"


def check_codex() -> str:
    """제품과 같은 방식(전용 CODEX_HOME, 헤드리스)으로 Codex를 한 번 부른다."""
    if not (CODEX_HOME / "config.toml").exists():
        raise RuntimeError("var/codex-home이 없다 (먼저 init)")
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--json",
             "-C", tmp, "Reply with exactly the word PONG."],
            env=agent_env(CODEX_HOME=str(CODEX_HOME)),
            capture_output=True, text=True, timeout=300,
        )
    events = [json.loads(l) for l in out.stdout.splitlines() if l.startswith("{")]
    text = " ".join(
        e.get("item", {}).get("text", "") for e in events if e.get("type") == "item.completed"
    )
    usage = next((e.get("usage") for e in events if e.get("type") == "turn.completed"), None)
    if out.returncode != 0 or "PONG" not in text:
        raise RuntimeError(f"응답 이상: {(out.stderr or out.stdout).strip()[-300:]}")
    model = tomllib.loads((CODEX_HOME / "config.toml").read_text()).get("model")
    record_doctor("codex", {"model": model, "usage": usage})
    return f"model={model} usage={usage}"


def check_browser() -> str:
    """합성 장면 렌더링에 필요한 헤드리스 Chromium과 WebGL을 확인한다."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = browser.new_page()
        webgl = page.evaluate("!!document.createElement('canvas').getContext('webgl2')")
        version = browser.version
        browser.close()
    if not webgl:
        raise RuntimeError("WebGL2 컨텍스트를 만들 수 없다")
    return f"chromium {version}, WebGL2 사용 가능"


def check_local() -> str:
    missing = [str(p.relative_to(ROOT)) for p in
               (CODEX_HOME / "config.toml", VENDOR / "three.module.js", PRODUCT / ".git")
               if not p.exists()]
    if missing:
        raise RuntimeError(f"없음: {', '.join(missing)} (먼저 init)")
    return "codex-home, vendor/three.js, product 저장소"


def check_linear() -> str:
    team = linear_team(team_key())
    labels = {l["name"] for l in team["labels"]["nodes"]}
    states = {s["name"] for s in team["states"]["nodes"]}
    missing = [n for n in LINEAR_LABELS if n not in labels]
    if NEEDS_HUMAN not in states:
        missing.append(f"상태 '{NEEDS_HUMAN}'")
    if missing:
        raise RuntimeError(f"팀 {team['key']} 연결은 되지만 준비 안 됨: {', '.join(missing)} (linear-setup)")
    return f"팀 {team['key']} ({team['name']}) 라벨·상태 준비 완료"


def record_doctor(name: str, value: dict) -> None:
    path = VAR / "doctor.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[name] = value
    VAR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = [
        ("도구", check_tools),
        ("로컬 준비", check_local),
        ("AWS", check_aws),
        ("Chromium", check_browser),
        ("Linear", check_linear),
        ("Claude Code", check_claude),
        ("Codex", check_codex),
    ]
    if args.skip_agents:
        checks = [c for c in checks if c[0] not in ("Claude Code", "Codex")]
    failed = 0
    for name, check in checks:
        try:
            print(f"OK    {name:<12} {check()}", flush=True)
        except Exception as e:  # 점검은 하나가 실패해도 나머지를 계속한다
            failed += 1
            print(f"FAIL  {name:<12} {e}", flush=True)
    print(f"\n{len(checks) - failed}/{len(checks)} 통과")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# linear-setup: 라벨, Needs Human 상태, 읽기·쓰기 시험
# ---------------------------------------------------------------------------

def cmd_linear_setup(args: argparse.Namespace) -> int:
    team = linear_team(args.team or team_key())
    labels = {l["name"] for l in team["labels"]["nodes"]}
    for name, (color, description) in LINEAR_LABELS.items():
        if name in labels:
            print(f"있음  라벨 {name}")
            continue
        linear(
            """mutation($input: IssueLabelCreateInput!) {
              issueLabelCreate(input: $input) { success } }""",
            {"input": {"teamId": team["id"], "name": name, "color": color,
                       "description": description}},
        )
        print(f"생성  라벨 {name}")

    if any(s["name"] == NEEDS_HUMAN for s in team["states"]["nodes"]):
        print(f"있음  상태 {NEEDS_HUMAN}")
    else:
        linear(
            """mutation($input: WorkflowStateCreateInput!) {
              workflowStateCreate(input: $input) { success } }""",
            {"input": {"teamId": team["id"], "name": NEEDS_HUMAN, "type": "started",
                       "color": "#f2994a",
                       "description": "eval을 통과하지 못해 사람의 판단을 기다리는 이슈"}},
        )
        print(f"생성  상태 {NEEDS_HUMAN} (type=started)")

    # 읽기·쓰기 시험: 이슈를 만들고 댓글을 단 뒤 지운다
    issue = linear(
        """mutation($input: IssueCreateInput!) {
          issueCreate(input: $input) { issue { id identifier } } }""",
        {"input": {"teamId": team["id"], "title": "[factory] linear-setup 연결 시험",
                   "description": "factory.py linear-setup이 만든 시험 이슈. 곧 삭제된다."}},
    )["issueCreate"]["issue"]
    linear(
        """mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }""",
        {"input": {"issueId": issue["id"], "body": "댓글 쓰기 시험"}},
    )
    linear("""mutation($id: String!) { issueDelete(id: $id) { success } }""", {"id": issue["id"]})
    print(f"시험  이슈 {issue['identifier']} 생성 → 댓글 → 삭제 성공")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="factory.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="로컬 준비: codex-home, three.js 사본, 제품 저장소, Chromium")
    p = sub.add_parser("doctor", help="환경 점검 (Claude Code·Codex를 한 번씩 호출한다)")
    p.add_argument("--skip-agents", action="store_true", help="모델 호출 점검을 건너뛴다")
    p = sub.add_parser("linear-setup", help="Linear 라벨·Needs Human 상태 준비와 읽기·쓰기 시험")
    p.add_argument("--team", help="Linear 팀 키 (기본 FACTORY_LINEAR_TEAM 또는 SWF)")

    args = parser.parse_args()
    handlers = {"init": cmd_init, "doctor": cmd_doctor, "linear-setup": cmd_linear_setup}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
