"""tiny previz 구현 패키지: 사진 한 장을 같은 구도의 three.js 프리비즈 장면 파일로 바꾼다.

모듈
  config        경로·한도·환경 변수 설정과 로그
  prompt        Codex에 주는 지시문과 출력 스키마
  codex         Codex 헤드리스 호출과 응답 해석, 실행 중인 프로세스 정리
  geometry/     측정값 → 3D 장면 명세 (카메라 보정, 물체 피팅, 방, 면 색)
  scene_file    장면 명세 → `export function build(THREE)` ES 모듈 텍스트
  images        업로드 이미지 정규화
  store         요청 기록 저장소
  worker        백그라운드 장면 생성 작업
  web           HTTP 라우팅과 응답
  cli           명령행 인자 처리와 서버 실행
"""
