# traffic

Foxtrot 무중단 마이그레이션의 트래픽 계층. Cloudflare Load Balancer·Tunnel 설정, 가중치를 바꾸는 트래픽 서버, Tailscale 사설망 정책, k6 무중단 검증을 담는다.

| 폴더 | 내용 |
| --- | --- |
| `server/` | 트래픽 서버 (FastAPI, 가중치 변경 API) |
| `testapp/` | Cloudflare 동작 실험용 앱 |
| `cloudflare/` | 터널·로드 밸런서 설정 기록 |
| `tailscale/` | ACL 정책 파일 |
| `k6/` | 부하 테스트 스크립트 |
| `docs/` | 설계 문서, 실험 기록 |

비밀값은 커밋하지 않는다. 필요한 키는 `.env.example`을 본다.
