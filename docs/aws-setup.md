# AWS 트래픽 실험 서버

2026-11-04, `foxtrot` CLI 프로필로 새로 구성했다. 다른 파트의 DMS 데모 VPC는 사용하지 않았다.

| 항목 | 값 |
| --- | --- |
| 계정 | `952561150367` |
| 리전 / AZ | `ap-northeast-2` / `ap-northeast-2a` |
| EC2 | `i-032647d867d2a7fbf` (`foxtrot-traffic-test-a`) |
| 사양 | `t4g.small`, Ubuntu 24.04 ARM64, 암호화 gp3 8GB |
| VPC | `vpc-0f684517b136999aa`, `10.10.0.0/16` |
| 퍼블릭 서브넷 | `subnet-0b5d7871be0c60b43`, `10.10.0.0/24` |
| 사설 IP | `10.10.0.98` |
| 보안 그룹 | `sg-0acc0dbdeddf0cee1`, 인바운드 없음 |
| 인터넷 게이트웨이 | `igw-03e96fd065aaecf19` |
| 퍼블릭 라우팅 테이블 | `rtb-08d61fd12bad38133`, 기본 경로는 IGW |
| EC2 역할 / 인스턴스 프로파일 | `foxtrot-traffic-ec2`, `AmazonSSMManagedInstanceCore` |
| 태그 | `Project=foxtrot`, `Owner=traffic`, `Purpose=traffic-test-a` |

공인 IPv4는 아웃바운드 연결용으로 할당했다. 앱은 `127.0.0.1:8000`에서만 수신한다.
IMDSv2 필수, CPU 크레딧은 standard. 데이터 서브넷·DB·NAT Gateway는 생성하지 않았다.

## 앱과 접속

기존 `testapp/make-init.sh aws`로 초기화 스크립트를 생성했다.
이번 배포의 Docker 종료 유예만 40초로 지정했다(6초 drain + 최대 30초 요청 + 여유).
컨테이너 이름은 `testapp`, `SERVED_BY=aws`, 재시작 정책은 `unless-stopped`다.

AWS 콘솔: EC2 → 해당 인스턴스 → 연결 → Session Manager.
CLI 접속은 로컬에 Session Manager 플러그인이 설치되어 있어야 한다.

```bash
aws ssm start-session --profile foxtrot --region ap-northeast-2 --target i-032647d867d2a7fbf
```

서버 안에서 확인:

```bash
sudo docker ps
curl -si http://127.0.0.1:8000/readyz
curl -s http://127.0.0.1:8000/api/ping
```

확인 결과(2026-11-04 19:56 KST): EC2 system/instance 상태 검사 모두 `ok`, SSM `Online`,
testapp `running`, `/readyz` HTTP 200 및 `X-Served-By: aws`, `/api/ping`의 `pong=true`를 확인했다.
SSM 확인 명령 ID: `94cbf4b1-9d35-4223-a1ad-e4bd35260a04`.
실제 Cloudflare 경유 요청·가중치 전환·부하 테스트는 아직 수행하지 않았다.

## Cloudflare 연결 완료 — 2026-11-05

AWS EC2에 `foxtrot-cloudflared` 컨테이너를 host network로 실행했다.
`aws.foxtrot.live` → Tunnel → `http://127.0.0.1:8000`으로 연결한다.
인바운드 보안 그룹은 계속 비어 있다.

- 터널 토큰: SSM Parameter Store Standard SecureString `/foxtrot/traffic/aws/tunnel-token`.
- EC2 역할에 해당 파라미터 하나의 `ssm:GetParameter`만 추가했다.
- EC2의 `/etc/foxtrot/cloudflared.token`은 root 전용(0600)이며 컨테이너에 읽기 전용으로 마운트한다.
- 실행 인자는 `--token-file`로 파일 경로만 전달한다. SSM 명령과 Docker 환경변수에 토큰을 넣지 않았다.
- 컨테이너는 `unless-stopped`, metrics는 `127.0.0.1:20241`에서만 수신한다.
- 이미지: `cloudflare/cloudflared@sha256:4955524aeaa379e154023d5a027c250f7b35041a2e7e950cf13f144bc9b9c5ba`.

터널 상태 `healthy`, LB 경유 요청 20건 모두 HTTP 200 및 `X-Served-By: aws`를 확인했다.
세부 구성은 [Cloudflare 연결 기록](cloudflare-setup.md) 참고.
트래픽 제어 API는 여전히 로컬에서만 실행하며 EC2에는 배포하지 않았다.
