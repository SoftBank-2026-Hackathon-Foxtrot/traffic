# Cloudflare 실제 연결 기록

2026-11-05 구성 기록. 처음 AWS 단일 대상으로 연결한 뒤 GCP를 추가했다. 각 단계의 검증 범위는 아래에 구분한다.

## 구성

`사용자 → https://app.foxtrot.live → Cloudflare LB → AWS/GCP pool → 각 Tunnel → 각 testapp`

| 항목 | 값 |
| --- | --- |
| Zone | `foxtrot.live` (`active`) |
| Zone ID | `a347c7267257bd2d1b3fe35d0d62ac72` |
| AWS Tunnel | `foxtrot-traffic-aws`, `6988cdcf-a206-4c88-9441-590e81a2bb61` |
| Published route | `aws.foxtrot.live` → `http://127.0.0.1:8000` |
| AWS pool | `foxtrot-traffic-aws`, `b26f0a79ed2d63568022f5b0ffd2b00d` |
| Pool endpoint | `6988cdcf-a206-4c88-9441-590e81a2bb61.cfargotunnel.com` |
| Endpoint Host header | `aws.foxtrot.live` |
| Monitor | `d868141e463da1596d45afd61f3fa678`, HTTPS 443 `/readyz`, 기대 응답 200 |
| Monitor interval | 60초, timeout 5초, retries 2, check region SEAS |
| LB | `app.foxtrot.live`, `6bf4a03e3d63cb3da4a19e6d0c69a606` |
| Routing | proxied, random, session_affinity=none |
| Weights / fallback | AWS 100% / AWS |

GCP VM과 두 번째 Tunnel·pool은 [GCP 연결 기록](gcp-setup.md) 참고.

## 1차 확인 — AWS 단일 대상

- `aws.foxtrot.live`와 `app.foxtrot.live`의 `/readyz`, `/api/ping`: HTTP 200, `X-Served-By: aws`, `CF-Cache-Status: DYNAMIC`.
- LB의 `/api/ping`에 별도 요청 20건: 모두 `200:aws`. 대규모 부하나 무중단 보장은 아니다.
- AWS Tunnel 상태 `healthy`, SEAS monitor에서 pool/origin `healthy=true`, 응답 코드 200.
- 로컬 API `GET /status`: 실제 Cloudflare 응답으로 200 및 AWS 100% 확인.
- 로컬 API `POST /operations`로 `{"type":"set_weights","weights":{"aws":100}}` 접수 후 실제 PATCH·GET 성공.
- 작업 ID `51338c48-4cd2-4311-998e-e852579d5f5e`, `SUCCEEDED`. 접수 약 0.25초, 작업 약 1.49초. 이 값은 전체 엣지 반영 시간 측정이 아니다.

## 2차 확인 — AWS/GCP 전환

GCP 앱과 Tunnel을 추가한 뒤 2026-11-05 실제 HTTP 실험을 수행했다.

- 50 RPS 목표의 60초 전환·롤백 실험: 3,000건, 오류 0건.
- 30초씩 가중치를 유지한 추가 실험: 6,000건, 오류 0건.
- 연결 재사용 / 매번 새 연결 비교: 4,000건, 오류 0건.
- AWS에서 시작한 30초 요청은 GCP 100% 설정 후에도 AWS에서 정상 완료됐다.
- 마지막 설정은 AWS 100%, GCP 0%, fallback AWS. 두 환경은 모두 실행 중이다.
- 혼합 비율은 짧은 구간·연결 조건에 따라 편차가 컸다. 목표 비율의 정확한 수렴이나 전 세계 반영 시간을 검증했다고 보지 않는다.

실측 수치와 한계는 [전환 실험 결과](traffic-verification-20261105.md) 참고.

## 현재 제어 API 실행

위 실측에서 사용한 `/status`와 `/operations`는 당시 경로다. 재현할 때는 [v1 실행·호출 안내](api.md)와 [전환 예제](../scripts/scenario.py)를 사용한다.

## 남은 것

- 실제 DB·Redis·로그인 세션을 포함한 전환 검증. testapp에는 상태 저장 기능이 없다.
- k6 기반 장시간·장애 주입·서버 종료 실험. 이번 확인은 Python httpx로 수행했다.
- Alloy·Prometheus·Grafana 관측 구성. 아직 배포하지 않았다.
- 현재 `traffic_verified=false` API 계약은 유지한다. 외부 실험 결과를 작업 성공 판정에 자동 통합하지 않았다.

참고: [Tunnel + LB 구성](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/public-load-balancers/).
