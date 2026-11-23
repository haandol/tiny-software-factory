"""tiny-software-factory의 구현. 진입점은 저장소 루트의 factory.py다.

모듈은 책임 단위로 나뉜다.

  config     경로와 실행 정책 (사람이 정한다)
  env        .env, Bedrock default profile, git
  linear     제어판: Linear GraphQL 클라이언트와 팀 준비
  setup      0단계: init, doctor
  oracle/    평가 (Evaluation Plane). factory-eval 이슈만 고친다
  harness    에이전트 지시문 불러오기 (harness/<버전>/*.md)
  worker     생산 루프: worktree, Claude Code, 재시도
  refactor   생산 루프의 리팩터링 단계
  delivery   병합과 로컬 배포
"""
