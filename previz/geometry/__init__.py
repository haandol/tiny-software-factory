"""기하: Codex 측정값을 3D 장면 명세로 푼다 (카메라 보정 → 물체 피팅 → 방 → 면 색 → 명세).

월드 좌표: y 위, 바닥 y=0, 카메라는 (0, h, 0)에서 수평 방향 -z를 본다 (방이 카메라 기준으로 놓인다).
카메라 자세는 three.js와 같다: lookAt(피치) 다음 rotateZ(롤).

모듈
  values       Codex 측정값 읽기 (유한한 숫자, 색, 픽셀 점)
  camera       카메라 모델(투영·역투영)과 Codex 추정값으로 만든 초기 카메라
  lm           작은 비선형 최소제곱 (Levenberg–Marquardt)
  calibration  측정값 하나로 카메라 보정 (Codex 추정값 + 벽 선 평행·직교 + 상자 직각)
  edges        사진의 경계선 찾기 (예상 선 둘레의 실제 경계점)
  ellipses     원기둥 윗면 타원으로 카메라 재기
  roomfit/     사진 경계로 카메라 맞추기 (모든 표본 + 사진 → 화각·피치·롤)
  objects      물체 피팅 (상자·원기둥을 bbox에 맞춤)
  shapes       물체 모양의 3D 점
  room         방 (벽 위치·높이)
  colors       면 색과 조명
  spec         장면 명세 조립
"""
