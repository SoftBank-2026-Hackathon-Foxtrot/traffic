# GCP 테스트 앱 연결

2026-11-05, 프로젝트 `foxtrot-513413`에 구성했다. 사용자 OAuth로 로컬 gcloud 구성 `foxtrot`을 사용하며 서비스 계정 키 파일은 만들지 않았다.

| 항목 | 값 |
| --- | --- |
| 프로젝트 / 번호 | `foxtrot-513413` / `540940380743` |
| VM / 존 | `foxtrot-traffic-test-b` / `asia-northeast3-a` |
| 사양 | `e2-small`, Ubuntu 24.04 x86_64, pd-balanced 10GB |
| VPC | `foxtrot-traffic-gcp` (custom subnet) |
| 서브넷 | `foxtrot-traffic-gcp-seoul`, `10.20.0.0/24` |
| 사설 / 공인 IP | `10.20.0.2` / `34.64.190.7` (공인 IP는 변경될 수 있음) |
| VM 서비스 계정 | `foxtrot-traffic-app@foxtrot-513413.iam.gserviceaccount.com` |
| Tunnel | `foxtrot-traffic-gcp`, `dcdc3044-beea-45a2-86cb-629a567b3a99` |
| Pool | `c02ba1a4acbbbb702fd2f67d6b6dc6a4` |
| Monitor | `612d4c975d17664d50b319a2e9d9c9ae`, HTTPS `/readyz`, 60초, SEAS |

## 실행과 보안 설정

- 테스트 앱은 Docker `testapp`, `SERVED_BY=gcp`, `127.0.0.1:8000`에만 바인딩한다. 종료 유예는 AWS와 동일한 40초다.
- `gcp.foxtrot.live` → Cloudflare Tunnel → `http://127.0.0.1:8000`.
- 인바운드 허용 방화벽 규칙 없음. SSH·앱 포트를 공개하지 않았다. 공인 IP는 아웃바운드 연결용이다.
- Secret Manager `foxtrot-traffic-gcp-tunnel-token`에 터널 토큰을 보관하며 VM 서비스 계정에는 이 시크릿 하나의 `roles/secretmanager.secretAccessor`만 부여했다.
- 시작 스크립트는 VM 메타데이터 자격 증명으로 토큰을 읽어 root 전용 파일(0600)에 저장한다. 토큰 자체는 VM 메타데이터·시작 스크립트·Docker 환경변수·명령줄에 넣지 않았다.
- `foxtrot-cloudflared`는 AWS와 같은 이미지 digest, host network, `--token-file`, `unless-stopped`를 사용한다.
- Shielded VM의 Secure Boot, vTPM, integrity monitoring 활성화.
- VM·디스크·공인 IP 등 사용료가 발생한다. 실험 종료 시 두 클라우드 자원과 LB 구독의 유지 여부를 결정해야 한다.

## 확인과 연결

VM 초기화 완료, 직접 도메인의 `/api/ping` HTTP 200 및 `X-Served-By: gcp` 확인.
첫 헬스체크는 터널 시작 전 `530 Unknown host`였으며, 연결 후 정상으로 전환됐다.
양쪽 pool이 healthy인 것을 확인한 뒤 GCP를 0%로 추가했다.
실제 분배·전환 결과는 [Cloudflare 연결 기록](cloudflare-setup.md) 참고.

CLI는 작업 폴더 `../tools/google-cloud-sdk`에 592.0.0을 설치했고 공식 SHA256을 검증했다. 시스템 PATH는 변경하지 않았다.

```powershell
# 저장소 루트에서 VM 상태 확인
& ..\tools\google-cloud-sdk\bin\gcloud.cmd compute instances describe foxtrot-traffic-test-b --zone=asia-northeast3-a --project=foxtrot-513413 --configuration=foxtrot
```
