"""제어판: Linear GraphQL 클라이언트와 팀 준비 (linear-setup)."""

from __future__ import annotations

import json
import os
import urllib.request

LINEAR_URL = "https://api.linear.app/graphql"
LABELS = {                               # 이름: (색, 설명)
    "factory": ("#5e6ad2", "팩토리가 제품 저장소에서 처리하는 이슈"),
    "factory-eval": ("#f2c94c", "팩토리가 평가 자산(tsf/oracle, CONTRACT.md)에서 처리하는 이슈"),
    "auto-discovered": ("#26b5ce", "발견 루프가 만든 이슈"),
}
NEEDS_HUMAN = "Needs Human"

# 트래커 비밀값의 환경 변수 이름. 에이전트·제품 하위 프로세스 환경에서 지운다 (Symphony SPEC §15.3과 같은 규칙).
# 하위 프로세스가 이 키를 가지면 이슈 본문에 섞인 지시로 팩토리를 거치지 않고 트래커를 바꿀 수 있다.
SECRET_ENV_NAMES = ["LINEAR_API_KEY"]


def linear(query: str, variables: dict | None = None) -> dict:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RuntimeError("LINEAR_API_KEY가 없다 (.env 확인)")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(LINEAR_URL, data=body,
                                 headers={"Authorization": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if data.get("errors"):
        raise RuntimeError(f"Linear 오류: {data['errors']}")
    return data["data"]


def team_key() -> str:
    return os.environ.get("FACTORY_LINEAR_TEAM", "SWF")


def linear_team(key: str | None = None) -> dict:
    data = linear(
        """query($key: String!) {
          teams(filter: { key: { eq: $key } }) { nodes {
            id key name
            states { nodes { id name type } }
            labels { nodes { id name } }
          } }
        }""",
        {"key": key or team_key()},
    )
    nodes = data["teams"]["nodes"]
    if not nodes:
        raise RuntimeError(f"Linear 팀 {key or team_key()}를 찾을 수 없다")
    return nodes[0]


def check_team() -> str:
    team = linear_team()
    labels = {l["name"] for l in team["labels"]["nodes"]}
    states = {s["name"] for s in team["states"]["nodes"]}
    missing = [n for n in LABELS if n not in labels]
    if NEEDS_HUMAN not in states:
        missing.append(f"상태 '{NEEDS_HUMAN}'")
    if missing:
        raise RuntimeError(f"팀 {team['key']} 연결은 되지만 준비 안 됨: {', '.join(missing)} (linear-setup)")
    return f"팀 {team['key']} ({team['name']}) 라벨·상태 준비 완료"


def setup_team(key: str | None = None) -> None:
    """라벨 3개와 Needs Human 상태를 만들고, 시험 이슈로 읽기·쓰기를 확인한다."""
    team = linear_team(key)
    labels = {l["name"] for l in team["labels"]["nodes"]}
    for name, (color, description) in LABELS.items():
        if name in labels:
            print(f"있음  라벨 {name}")
            continue
        linear("""mutation($input: IssueLabelCreateInput!) { issueLabelCreate(input: $input) { success } }""",
               {"input": {"teamId": team["id"], "name": name, "color": color, "description": description}})
        print(f"생성  라벨 {name}")

    if any(s["name"] == NEEDS_HUMAN for s in team["states"]["nodes"]):
        print(f"있음  상태 {NEEDS_HUMAN}")
    else:
        linear("""mutation($input: WorkflowStateCreateInput!) { workflowStateCreate(input: $input) { success } }""",
               {"input": {"teamId": team["id"], "name": NEEDS_HUMAN, "type": "started", "color": "#f2994a",
                          "description": "eval을 통과하지 못해 사람의 판단을 기다리는 이슈"}})
        print(f"생성  상태 {NEEDS_HUMAN} (type=started)")

    issue = linear("""mutation($input: IssueCreateInput!) { issueCreate(input: $input) { issue { id identifier } } }""",
                   {"input": {"teamId": team["id"], "title": "[factory] linear-setup 연결 시험",
                              "description": "factory.py linear-setup이 만든 시험 이슈. 곧 삭제된다."}}
                   )["issueCreate"]["issue"]
    linear("""mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }""",
           {"input": {"issueId": issue["id"], "body": "댓글 쓰기 시험"}})
    linear("""mutation($id: String!) { issueDelete(id: $id) { success } }""", {"id": issue["id"]})
    print(f"시험  이슈 {issue['identifier']} 생성 → 댓글 → 삭제 성공")


# --- 어댑터: 이슈 읽기, 상태 이동, 댓글, 첨부 ---------------------------------------
# Symphony SPEC §11처럼 읽기는 상태 목록 조회와 ID 조회 두 가지다. 쓰기는 에이전트가 아니라 컨트롤러만 한다.

FACTORY_MARK = "**[factory]**"           # 팩토리가 쓴 댓글의 머리. 사람 댓글과 구분한다

ISSUE_FIELDS = """id identifier title description priority createdAt url
  state { name type } labels { nodes { name } }
  comments(first: 100) { nodes { body createdAt } }"""


def normalize(node: dict) -> dict:
    return {"id": node["id"], "identifier": node["identifier"], "title": node["title"],
            "description": node.get("description") or "", "priority": node.get("priority"),
            "created_at": node.get("createdAt"), "url": node.get("url"), "state": node["state"]["name"],
            "labels": sorted({l["name"].strip().lower() for l in node["labels"]["nodes"]}),
            "comments": sorted(node["comments"]["nodes"], key=lambda c: c["createdAt"])}


def fetch_issues(states: list[str]) -> list[dict]:
    if not states:
        return []
    data = linear(f"""query($key: String!, $states: [String!]) {{
        issues(first: 100, filter: {{ team: {{ key: {{ eq: $key }} }}, state: {{ name: {{ in: $states }} }} }})
        {{ nodes {{ {ISSUE_FIELDS} }} }} }}""", {"key": team_key(), "states": states})
    return [normalize(n) for n in data["issues"]["nodes"]]


def fetch_issue(issue_id: str) -> dict | None:
    data = linear(f"""query($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS} }} }}""", {"id": issue_id})
    return normalize(data["issue"]) if data.get("issue") else None


def move(issue: dict, state_name: str) -> None:
    states = {s["name"]: s["id"] for s in linear_team()["states"]["nodes"]}
    linear("""mutation($id: String!, $state: String!) { issueUpdate(id: $id, input: { stateId: $state }) { success } }""",
           {"id": issue["id"], "state": states[state_name]})


def comment(issue: dict, body: str) -> None:
    linear("""mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }""",
           {"input": {"issueId": issue["id"], "body": f"{FACTORY_MARK} {body}"}})


def upload(path) -> str | None:
    """파일을 Linear 저장소에 올리고 댓글에 넣을 수 있는 주소를 돌려준다. 실패하면 None."""
    from pathlib import Path
    path = Path(path)
    if not path.exists():
        return None
    data = path.read_bytes()
    try:
        up = linear("""mutation($type: String!, $name: String!, $size: Int!) {
            fileUpload(contentType: $type, filename: $name, size: $size) {
              success uploadFile { uploadUrl assetUrl headers { key value } } } }""",
                    {"type": "image/png", "name": path.name, "size": len(data)})["fileUpload"]["uploadFile"]
        headers = {"Content-Type": "image/png", "Cache-Control": "public, max-age=31536000"}
        headers.update({h["key"]: h["value"] for h in up["headers"]})
        req = urllib.request.Request(up["uploadUrl"], data=data, method="PUT", headers=headers)
        urllib.request.urlopen(req, timeout=60).close()
        return up["assetUrl"]
    except Exception as e:  # 첨부 실패는 실행 결과에 영향을 주지 않는다
        print(f"  첨부 업로드 실패 ({path.name}): {e}")
        return None


def create_issue(title: str, description: str, labels: list[str] = (), state: str = "Todo") -> dict:
    team = linear_team()
    label_ids = [l["id"] for l in team["labels"]["nodes"] if l["name"] in labels]
    state_id = next(s["id"] for s in team["states"]["nodes"] if s["name"] == state)
    return linear("""mutation($input: IssueCreateInput!) { issueCreate(input: $input) { issue { id identifier url } } }""",
                  {"input": {"teamId": team["id"], "title": title, "description": description,
                             "labelIds": label_ids, "stateId": state_id}})["issueCreate"]["issue"]
