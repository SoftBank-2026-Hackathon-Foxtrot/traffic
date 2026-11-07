# 마이그레이션 관측

공개 작업 API에 startedAt/finishedAt, 상태 API에 observedAt을 추가했습니다. 컨트롤 플레인은 migrationId/transitionId를 전달하고 실제 operationId와 결과 시각을 기록합니다.

weights/effectivePercent/configVerified는 Cloudflare LB 설정을 다시 읽은 결과입니다. 실제 사용자 요청 비율이나 무중단의 증명이 아닙니다. 실제 도착 위치는 컨트롤 플레인의 별도 공용 probe 표본에서 표시합니다. 기존 traffic_verified=false의 의미는 유지합니다.

검사: `python -m unittest discover -s tests -v`. OpenAPI 갱신: `python -m scripts.export_openapi`.
