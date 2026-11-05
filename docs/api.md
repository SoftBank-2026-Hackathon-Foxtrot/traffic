# 트래픽 제어 API — 최소 구현

FastAPI + httpx + SQLite. 플랫폼은 아래 v1 API로 가중치 변경·롤백·불명 작업 대조를 호출한다.

## 컨테이너로 실행

플랫폼·COMPUTE가 호출할 제어 서버는 저장소 루트의 `compose.yaml`로 실행한다. 기존 `../.env`에 `TRAFFIC_SERVICE_TOKEN`(호출자 인증용)과 아래 Cloudflare 설정을 넣는다. Compose는 제어 서버에 필요한 값만 전달한다.

```bash
docker compose --env-file ../.env up -d --build --wait
curl http://127.0.0.1:8001/health
```

다른 로컬 프로세스가 8001을 사용하면 `../.env`에 `TRAFFIC_HOST_PORT=18001`을 지정하고 그 포트로 호출한다. 같은 Compose 네트워크의 플랫폼은 `http://traffic:8001`로 호출할 수 있다. 보호된 v1 경로에는 호출자의 `Authorization: Bearer <TRAFFIC_SERVICE_TOKEN>`이 필요하다.

SQLite는 명명된 볼륨 `traffic-data`의 `/var/lib/foxtrot/traffic/traffic.db`에 보관한다. 같은 Compose 프로젝트 이름으로 실행하면 컨테이너 교체 후에도 이력과 중복 방지 키를 사용한다. 중지할 때는 `docker compose --env-file ../.env down`을 사용한다. `down -v`는 볼륨도 삭제한다. 첫 실행은 빈 DB로 시작한다. 기존 로컬 이력을 이어 쓸 때는 이전 서버를 먼저 종료하고 SQLite 백업을 볼륨에 복원한 뒤 시작한다.

**관리 VPC 배포 시 전달할 설정**

| 항목 | 값 |
| --- | --- |
| 이미지 | 저장소 루트에서 `docker build -t foxtrot-traffic ./server`로 빌드한 이미지를 팀 ECR에 게시 |
| 서비스 | 컨테이너 포트 `8001`, 헬스체크 `GET /health`, 사설 주소를 플랫폼에 등록. 플랫폼·COMPUTE만 접근할 수 있는 사설 HTTPS 경로 사용 |
| 비밀값 | Secrets Manager에서 `TRAFFIC_SERVICE_TOKEN`과 `CF_API_TOKEN` 주입. 나머지 네 가지 Cloudflare 설정은 아래 참조 |
| 상태 | 재배포 시 같은 DB를 다시 연결하는 영속 마운트 필요. `/var/lib/foxtrot/traffic`에 UID/GID `10001:10001`의 쓰기 권한 부여 |
| 교체 | 단일 태스크·단일 Uvicorn worker. 배포 시 이전 태스크가 종료된 뒤 새 태스크를 시작해 같은 LB를 동시에 변경하지 않도록 구성 |

Compose 볼륨은 해당 Docker 호스트에 있다. ECS의 태스크 간 저장소와 사설 서비스 주소는 팀 IaC에서 별도로 연결한다.

## Python으로 실행

저장소 루트에서 Python 3.11+ 가상환경을 활성화한 뒤 실행한다.

```bash
python -m pip install -r server/requirements.txt
python -m uvicorn server.main:app --env-file ../.env --host 127.0.0.1 --port 8001 --workers 1
```

`.env.example`을 참고해 저장소 밖 `../.env`에 설정한다. 필요한 값:

- `TRAFFIC_SERVICE_TOKEN`: 플랫폼·COMPUTE 호출자 인증용 Bearer 토큰
- `CF_API_TOKEN`: LB 수정, pool 헬스 읽기 권한
- `CF_ACCOUNT_ID`, `CF_ZONE_ID`, `CF_LB_ID`
- `CF_POOL_IDS`: `{"aws":"<pool ID>","gcp":"<pool ID>"}`
- `TRAFFIC_DB_PATH`: 선택값, 기본 `server/traffic.db`

설정이 없어도 서버와 `/health`, `/docs`는 열린다. Cloudflare를 사용하는 API는 설정이 없으면 503을 반환한다.
LB는 enabled, proxied, random steering, session affinity none으로 준비하고, `CF_POOL_IDS`에는 `default_pools` 전체를 등록한다. 비활성 LB 및 지역별 라우팅·사용자 정의 규칙·pool sets가 있는 구성은 가중치를 변경하기 전에 거절한다.

## 플랫폼·COMPUTE 연동

[마이그레이션 공통 Design Doc (2), 초안 0.2](https://app.notion.com/p/Design-Doc-2-40fb7958f32780ee9403e6284e1c3210)의 실행 규칙을 적용한 **트래픽 전용 하위 API**다. 플랫폼 또는 COMPUTE가 이 서비스를 호출해 Cloudflare의 가중치를 한 단계 바꾼다. `/v1/traffic/actions`와 아래 명령 이름은 이 저장소에서 정의한 확장이다. 공통 `/v1/migrations/{id}/actions`와 COMPUTE의 QUIESCE·CUTOVER·RESUME·readiness.advance는 상위 어댑터의 책임이다. 특히 CUTOVER에서 실제 업무를 정지한 채 경로만 바꾸려면 호출자가 그 정지·배수 근거를 먼저 확인해야 한다.

단일 LB, 단일 실행 프로세스, 전용 SQLite 파일을 사용한다. 별도 서비스 자격 증명 `TRAFFIC_SERVICE_TOKEN`을 설정한다. 플랫폼 연결은 사설 HTTPS 경로에서 제공하고 같은 DB를 동시에 사용하는 서버를 늘리지 않는다. 재시작·재배포 시 DB 파일을 유지해야 기존 키와 revision이 보존된다. 자격 증명은 한 신뢰 주체에 발급하며 플랫폼과 COMPUTE 중 지정한 실행 주체 하나가 명령을 직렬화한다. 로컬·컨테이너 모두 상태 조회, 변경, 결과 조회에 같은 Bearer 인증을 사용한다.

1. `Authorization: Bearer <서비스 토큰>`으로 `GET /v1/traffic/status`를 호출한다. `weights`, `pool_health`, `revision`, `unknownOperationIds`를 확인한다. revision은 이 API에서 접수한 작업 수로, 직접 대시보드 변경은 감지하지 않는다.
2. `POST /v1/traffic/actions`에 같은 인증 헤더와 `Idempotency-Key`를 넣는다. 아래 예의 revision은 0이며 실제로는 조회값을 사용한다. migrationId·transitionId는 상위 계획의 추적용 값이다. 롤백에는 되돌릴 작업과 동일한 값을 넣는다.
3. 202 응답의 `Location` 또는 `statusUrl`을 GET 한다. `Retry-After`의 초 단위를 따라 QUEUED/RUNNING 동안 조회한다. SUCCEEDED는 설정 재조회 성공이다. 실제 앱 분배와 오류는 별도 관측 후 상위 호출자가 판단한다.

```json
{"action":"SET_WEIGHTS","expectedRevision":0,"migrationId":"mig-app-01","transitionId":"tx-01","weights":{"aws":95,"gcp":5}}
```

| 명령 | 실행 내용 |
| --- | --- |
| SET_WEIGHTS | 설정한 모든 환경에 대한 목표 비율 적용 |
| ROLLBACK | 같은 범위의 마지막 적용 작업 이전 가중치 복원. 호출자는 현재 데이터 경로와 원본 앱의 호환성을 먼저 검사 |
| RECONCILE | 이전 단일 실행 주체의 종료와 더 이상 늦은 변경이 없음을 호출자가 확인한 뒤, 해당 근거 참조와 UNKNOWN 작업 ID를 제출. Cloudflare GET으로만 대조 |

같은 키·같은 본문은 재시작 후에도 최초 접수 결과를 반환한다. 같은 키·다른 본문 또는 새 요청의 오래된 revision은 409다. 동일한 LB에 진행 중인 작업이 있으면 RESOURCE_BUSY, 미해결 UNKNOWN이 있으면 UNKNOWN_STATE다. 재전송 시 최초 본문과 키를 그대로 보낸다.

복구 예시 (revision은 다시 조회):

```json
{"action":"RECONCILE","expectedRevision":1,"unknownOperationId":"<작업 ID>","executorStoppedRef":"incident:previous-executor-stopped"}
```

executorStoppedRef는 호출자가 검증해 제공하는 근거 참조다. 서비스가 외부 명령 종료를 독립 검증하지 않으므로 확인 전에는 제출하지 않는다. 실제 가중치가 요청과 일치하면 APPLIED, 변경 전 값과 일치하거나 PATCH 전 중단 근거가 있으면 NO_CHANGE로 기록한다. 둘 다 아니거나 조회가 실패하면 기존 UNKNOWN 차단을 유지한다. 최초 UNKNOWN 기록은 보존하며 복구 작업으로 해결 결과를 추적한다. timeout만으로 재전송·롤백·업무 재개를 수행하지 않는다.


### Design Doc (2) 대조 — 2026-11-06

문서 본문은 공통 초안 0.2 / HTTP v1이다. 공통 리소스 종류는 DATABASE, REDIS, OBJECT_STORAGE, COMPUTE이며 Cloudflare 가중치 전용 명령을 별도로 표준화하지 않는다. 현재 팀에서 정한 범위대로 traffic은 플랫폼·COMPUTE가 호출하는 하위 서비스다.

| 설계의 조건 | traffic의 구현 및 호출자 책임 |
| --- | --- |
| CUTOVER와 RESUME를 분리 | traffic은 LB 비율 변경 결과만 보고한다. 업무 정지 근거(quiesceRef), 의존 서비스 확인, 앱·Worker 재개는 상위에서 처리한다. |
| readiness.advance와 유효한 근거 | 공통 초안은 필수 검사 결과와 30초 안정 구간, 10초 이하 관측 간격을 요구한다. 상위에서 판단한다. 데모의 대기·HTTP 표본과 traffic의 SUCCEEDED는 이 조건의 일부만 관찰한다. |
| 한 계획에 transitionId 하나 | 하위 API의 migrationId·transitionId는 추적 및 롤백 범위 비교용이다. 전체 계획의 ID 발급·유일성·세대·종료는 상위에서 관리한다. |
| FAILED 및 UNKNOWN 후 복구 | 공통 어댑터는 FAILED 후에도 RECONCILE로 종료·현재 조건을 확인한다. traffic은 변경 전 실패를 FAILED로 기록하며 다음 호출을 허용하고, RECONCILE은 미해결 UNKNOWN에만 제공한다. 상위는 FAILED 후 진행을 멈추고 자체 복구 판단을 거친다. UNKNOWN에서는 이전 실행의 잔여 변경 가능성을 확인한 주체가 executorStoppedRef를 제공한다. |
| 업무 복귀 승인 | traffic ROLLBACK은 해당 작업 전 가중치를 복원한다. 실제 워크플로에서 호출하기 전에는 상위가 업무 정지·데이터 최신성·원본 앱과 현재 저장소의 호환성을 검증한다. |

플랫폼용 [정적 OpenAPI](../api/openapi.json) · [테스트 앱 데모](../scripts/scenario.py). 표준 어댑터의 계획 등록·공통 actions 경로와 이 서비스의 `/v1/traffic/actions`를 구분해 호출한다.

## 완료 의미와 제한

- `QUEUED` → `RUNNING` → `SUCCEEDED` / `FAILED` / `UNKNOWN`.
- `SUCCEEDED`는 Cloudflare 설정을 재조회해 요청한 비율과 일치함을 확인했다는 뜻이다. 실제 요청 분배와 무중단은 검증하지 않으며 결과의 `traffic_verified`는 `false`다.
- PATCH 시도 이후 오류 또는 실행 중 서버 재시작은 `UNKNOWN`으로 기록한다. `/v1/traffic/status`와 Cloudflare에서 실제 상태를 확인한 뒤 다음 명령을 결정한다. 자동 재시도는 없다.
- 단일 프로세스, 한 번에 작업 하나. SQLite에는 명령·이력·중복 접수 기록을 함께 보관하며 별도 작업 큐는 없다.
- `drain`(요청 소진 확인), Analytics/telemetry, 관리 VPC의 원격 배포는 후속 작업이다.
- 가중치 0은 요청 소진 완료를 뜻하지 않는다. 이 API는 앱이나 터널을 종료하지 않는다.

Cloudflare API 참고: [LB PATCH](https://developers.cloudflare.com/api/resources/load_balancers/methods/edit/), [pool health](https://developers.cloudflare.com/api/resources/load_balancers/subresources/pools/subresources/health/methods/get/).

## 명세 생성과 로컬 검증

저장소 루트에서 실행한다. Swagger의 **Authorize**에 서비스 토큰을 입력하면 보호된 경로를 호출할 수 있다. 런타임 모델을 수정하면 같은 코드에서 배포용 명세를 다시 생성한다.

```bash
python -m scripts.export_openapi
python -m unittest discover -s tests -v
```

테스트는 임시 SQLite와 실제 v1 라우트를 사용하며 Cloudflare HTTP 응답을 모의한다. 인증, 입력 오류, 동시 실행 차단, 가중치 변경·롤백, 중복 접수, UNKNOWN 복구, 저장된 명세와 런타임 명세의 일치를 검사한다.

이전 단일 AWS 테스트와 2개 환경 전환 실측은 [Cloudflare 연결 기록](cloudflare-setup.md)과 [실험 결과](traffic-verification-20261105.md)에 보관한다. 저장소 밖의 초기 실험 스크립트는 당시 API 전용이다. 현재 실행 예제는 [scripts/scenario.py](../scripts/scenario.py)다.
