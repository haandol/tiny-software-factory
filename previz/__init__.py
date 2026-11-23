"""tiny previz 구현 패키지: 사진 한 장을 같은 구도의 three.js 프리비즈 장면 파일로 바꾼다.

모듈
  config        경로·한도·환경 변수 설정과 로그
  prompt        Codex에 주는 지시문과 출력 스키마
  codex         Codex 측정 호출 (이미지 + 지시문 → 측정값 JSON). 요청마다 표본 여러 개를 동시에 잰다
  consensus     표본마다 장면을 풀고, 카메라 보정이 가장 잘 맞는 표본 하나를 고른다
  app_server    앱이 함께 쓰는 codex app-server: 띄우기·다시 띄우기·종료, 표본마다 스레드 하나에 turn 하나
  rpc           codex app-server 프로세스 하나와의 표준 입출력 JSON-RPC 연결
  geometry/     측정값 → 3D 장면 명세 (사진 경계로 카메라 맞추기, 카메라 보정, 물체 피팅, 방, 면 색)
  scene_file    장면 명세 → `export function build(THREE)` ES 모듈 텍스트
  images        업로드 이미지 정규화
  store         요청 기록 저장소
  worker        백그라운드 장면 생성 작업
  web           HTTP 라우팅과 응답
  cli           명령행 인자 처리와 서버 실행
"""
