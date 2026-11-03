# testapp

Cloudflare 로드 밸런서 실험용 대역 앱. 저장소가 없어서 실험 중 실패가 나면 트래픽 계층 문제로 볼 수 있다. 트래픽 계층이 기대는 동작은 실제 공구 앱(`demo/app/main.py`)과 같게 맞췄다.

| 경로 | 응답 |
| --- | --- |
| `GET /` | `{"served_by", "host"}` |
| `GET /readyz` | 평소 200. 종료 중이거나 `NOT_READY_FILE`이 있으면 503 |
| `GET /api/ping` | 경로 라우팅 확인용 |
| `GET /slow?seconds=N` | N초(최대 30) 뒤 응답. 드레이닝 중 처리 중인 요청이 살아남는지 확인용 |

모든 응답에 `X-Served-By: <SERVED_BY>` 헤더가 붙는다.

| 환경변수 | 기본값 | 뜻 |
| --- | --- | --- |
| `SERVED_BY` | `local` | 환경 이름 (`aws`, `gcp`, `onprem`) |
| `DRAIN_S` | `6` | SIGTERM 후 요청을 계속 받으며 로드 밸런서에서 빠지기를 기다리는 시간 |
| `NOT_READY_FILE` | `/tmp/testapp-not-ready` | 이 파일이 있으면 `/readyz`가 503 |

## 실행

```bash
docker build -t foxtrot-testapp ./testapp
docker run -d --name testapp --restart unless-stopped \
  -p 127.0.0.1:8000:8000 -e SERVED_BY=aws --stop-timeout 35 foxtrot-testapp
```

- `127.0.0.1`에만 열어 같은 서버의 cloudflared만 접근하게 한다.
- `--stop-timeout 35`: `docker stop`의 기본 유예 10초로는 드레이닝 도중 강제 종료될 수 있다. 앱 파트에 요청한 종료 유예(35초 이상)와 같게 둔다.

헬스체크 실패 흉내: `docker exec testapp touch /tmp/testapp-not-ready`, 되돌리기: `docker exec testapp rm /tmp/testapp-not-ready`

## 서버에 올리기

레포가 비공개라 서버에서 clone할 수 없으므로, testapp 파일을 담은 초기화 스크립트를 만들어 VM 생성 때 넘긴다(AWS `user_data`, GCP `startup-script`). 비밀값은 들어가지 않는다.

```bash
testapp/make-init.sh aws > init-aws.sh    # gcp, onprem도 같다
```

생성된 스크립트는 Docker(`docker.io`)를 설치하고 testapp을 빌드해 위 명령으로 실행한다. GCP는 부팅마다 다시 실행하므로 이미 있으면 건너뛴다.

마지막에 `testapp-check: readyz=<코드> x-served-by=<환경>`을 부팅 로그에 남기므로 셸 없이 확인할 수 있다.

```bash
aws ec2 get-console-output --instance-id <ID> --latest --output text | grep testapp-check
gcloud compute instances get-serial-port-output <이름> --zone <존> | grep testapp-check
```

확인(2026-11-03): EC2 t4g.small(arm64)에서 부팅 약 170초, GCE e2-small(x86)에서 약 75초 뒤 `readyz=200`.

## 종료 순서

SIGTERM → `/readyz` 즉시 503, 다른 요청은 `DRAIN_S` 동안 정상 처리 → 새 연결 거부, 처리 중인 요청은 끝까지 처리 → 종료.

로컬 확인(2026-11-03, 맥 Docker, `DRAIN_S=6`): `docker stop` 후 6초 동안 `/readyz` 503·`/api/ping` 200, 그 뒤 새 연결 거부, 그 전에 시작한 8초짜리 요청은 200으로 완료. 컨테이너는 `docker stop` 후 약 7초에 종료 코드 0으로 꺼졌다(강제 종료 없음).
