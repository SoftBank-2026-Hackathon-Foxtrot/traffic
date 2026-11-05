# traffic

Foxtrot 중앙 플랫폼 또는 COMPUTE가 AWS·GCP 테스트 앱으로 들어가는 트래픽 비율을 조절하는 HTTP 서버다. 가중치를 Cloudflare에 적용하고 설정을 다시 읽은 결과와 작업 이력을 반환한다.

## 실행

1. 저장소 상위의 `../.env`에 `TRAFFIC_SERVICE_TOKEN`, `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `CF_ZONE_ID`, `CF_LB_ID`, `CF_POOL_IDS`를 설정한다. 환경 이름은 `aws`와 `gcp`를 사용한다. [설정 예시](.env.example)
   호출자는 `TRAFFIC_SERVICE_TOKEN`을 사용하고, 제어 서버가 `CF_API_TOKEN`으로 Cloudflare를 호출한다.
2. 저장소 루트에서 `docker compose --env-file ../.env up -d --build --wait`를 실행한다.
3. [생존 확인](http://127.0.0.1:8001/health) · [Swagger UI](http://127.0.0.1:8001/docs) · [플랫폼용 OpenAPI 파일](api/openapi.json)

## 플랫폼 연동과 검증

| 순서 | 호출 | 확인할 값 |
| --- | --- | --- |
| 1 | `GET /v1/traffic/status` | Bearer 토큰으로 현재 가중치·pool 상태·revision 조회 |
| 2 | `POST /v1/traffic/actions` | `Idempotency-Key`와 `{"action":"SET_WEIGHTS","expectedRevision":0,"weights":{"aws":95,"gcp":5}}` 전송. 예시의 0은 실제 조회한 revision으로 교체 |
| 3 | 202 응답의 `Location`을 GET | SUCCEEDED 후 실제 앱의 `X-Served-By`·오류 수 확인. 응답 유실 시 같은 키·본문으로 재조회. FAILED면 오류 확인, UNKNOWN이면 변경 중단 후 대조 |
| 4 | 동일 경로에 `ROLLBACK` | 승인된 복귀 시 새 키·현재 revision·원 변경과 같은 범위로 접수하고 결과 조회 |

**테스트 앱 데모.** Python 3.11+ 가상환경에서 `python -m pip install -r server/requirements.txt` 후 아래를 실행한다. 두 번째 명령은 해당 LB의 실제 가중치를 바꾸므로 전용 테스트 앱에서만 실행한다.

```bash
python scripts/scenario.py
python scripts/scenario.py --apply
```

[전환·롤백 예제](scripts/scenario.py)는 AWS 100%와 양쪽 pool의 정상 상태를 확인한 후 95:5를 적용한다. 기본 30초 대기 후 새 연결 100건의 응답을 집계하고 계획된 롤백으로 시작 가중치를 복원한다. 표본에 GCP가 없으면 관측 미완료로 종료한다. 명령 결과 불명·동시 변경 시에는 다음 변경을 중단하므로 출력한 작업 ID·키·본문과 실제 상태를 확인한다. 로컬 API 회귀 테스트는 `python -m unittest discover -s tests -v`로 실행한다.

**실측과 배포 상태.** 2026-11-05 전환·비율 유지·연결 조건 실험에서 HTTP 13,000건 중 실패 0건을 관찰했다. 이 수치는 당시 실험의 합계다. 제어 API의 관리 VPC 배포, 상시 관측, 서버 종료·세션 유지 검증은 후속 작업이다. [실측 결과](docs/traffic-verification-20261105.md) · [API 계약과 복구 절차](docs/api.md) · [AWS](docs/aws-setup.md) · [GCP](docs/gcp-setup.md) · [Cloudflare](docs/cloudflare-setup.md)

온프렘 경로는 설계 단계다. [초기 설계·구축 기록](docs/initial-design.md) · [검증용 앱](testapp/README.md)
