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

## 종료 순서

SIGTERM → `/readyz` 즉시 503, 다른 요청은 `DRAIN_S` 동안 정상 처리 → 새 연결 거부, 처리 중인 요청은 끝까지 처리 → 종료.

로컬 확인(2026-11-03, 맥 Docker, `DRAIN_S=6`): `docker stop` 후 6초 동안 `/readyz` 503·`/api/ping` 200, 그 뒤 새 연결 거부, 그 전에 시작한 8초짜리 요청은 200으로 완료. 컨테이너는 `docker stop` 후 약 7초에 종료 코드 0으로 꺼졌다(강제 종료 없음).
