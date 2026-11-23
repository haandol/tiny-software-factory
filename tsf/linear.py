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
