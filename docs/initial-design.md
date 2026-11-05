# 초기 설계와 구축 기록

인수 당시의 설계, 계획, 환경별 구축 명령을 보존한다. 현재 구현 상태와 실행 순서는 [README](../README.md), 현재 호출 계약은 [API 안내](api.md)를 따른다. 명령의 기준 디렉터리는 저장소 루트다.

## 1. 목표

맡은 일:

1. **트래픽 계층**: 사용자 요청을 AWS·GCP·온프레미스 중 어느 환경으로 보낼지 정하고, 이전 중 그 비율을 단계적으로 옮김. 사용자 주소와 DNS는 이전 내내 바꾸지 않음
2. **사설망**: 각 환경의 앱 → 관리 VPC의 DB·Redis 프록시, 관리 VPC → 각 클라우드의 관리형 DB·Redis 연결. 서버에 들어오는 포트는 열지 않음

**성공 기준**: k6 초당 50건 부하에서 AWS → GCP 이전 전체 과정 동안 **사용자 요청 실패 0건**. 컷오버 순간의 짧은 응답 지연은 허용(DB 파트 로컬 실측 전환 대기 165ms)

담당 범위:

- Cloudflare: 도메인(zone), Load Balancer, pool, 헬스체크(monitor), Tunnel, Access
- 트래픽 서버: 다른 파트 서버가 호출하는 가중치 변경 API
- Tailscale: tailnet, ACL, 인증 키, 서브넷 라우터 설정
- VPC 대역 설계
- k6 무중단 검증

---

## 2. 전체 구조

```
사용자 · k6
   │ HTTPS  app.<도메인> (이전 중 DNS 변경 없음)
Cloudflare Load Balancer ── 가중치(random) · 헬스체크 GET /readyz
   │ Cloudflare Tunnel (각 앱 VM의 cloudflared가 Cloudflare로 바깥 방향 연결을 유지)
   ├─ AWS 앱 EC2        ┐
   ├─ GCP 앱 GCE        ├─ 앱의 DB·Redis 접속은 Tailscale 경유 → 관리 VPC의 프록시
   └─ 온프레미스 맥미니  ┘
관리 VPC (AWS 10.30.0.0/16, 이전 대상 아님)
   PgBouncer db-proxy.internal:6432 · Redis 프록시 redis-proxy.internal:6379
   Migration Worker · 관리 DB · 트래픽 서버 · cloudflared(platform)
```

이전 단계별 트래픽 파트 역할 (가중치는 AWS:GCP):

| 단계 | 가중치 | 트래픽 파트 | 같은 시점의 다른 파트 |
| --- | --- | --- | --- |
| 0 준비 | 100:0 | pool·헬스체크 등록 | DB 논리 복제 준비, 앱은 Redis 프록시만 사용 |
| 1 데이터 복제 | 100:0 | 변화 없음 | 초기 복사 → CDC, Redis 동기화, S3 → GCS 복사 |
| 2 GCP 앱 배포 | 100:0 | GCP 터널 연결, 헬스체크 통과 확인 | 계속 |
| 3 카나리 | 95:5 | 요청 단위로 GCP에 5% | GCP 앱도 PgBouncer·Redis 프록시 경유로 AWS 데이터 사용 |
| 4 컷오버 | 95:5 유지 | **가중치 변경 없음** | PgBouncer·Redis 프록시 PAUSE → 전환 → RESUME |
| 5 확대 | 75:25 → 50:50 | 단계마다 실패율·p95 관찰 | 큐 drain |
| 6 GCP 100% | 0:100 | AWS 앱은 0%로 롤백 대기 | 역방향 동기화 유지 |
| 7 정리 | 0:100 | 환경 빼기 절차(3.4)로 AWS 제거 | RDS·S3 정리 |

- 컷오버 전에는 GCP 앱이 AWS의 DB를, 컷오버 후에는 AWS 앱이 GCP의 DB를 다른 클라우드 너머로 사용
- 그래서 **컷오버 전 비율은 작게, 컷오버 후에는 빠르게** 올림
- 컷오버 뒤 트래픽은 되돌릴 수 있지만 DB 자동 역이전은 없음

---

## 3. 설계와 이유

### 3.1 진입점: Cloudflare Load Balancer + Tunnel

| 결정 | 이유 |
| --- | --- |
| 진입점은 Cloudflare Load Balancer | 처음 안(Azure VM의 Envoy 1대)은 단일 장애점이고 비용 정산이 따로 필요했음. Cloudflare는 이전 대상 클라우드 바깥에 있어 이전 중에도 진입점이 그대로임. DNS를 바꾸지 않고 가중치만 바꿔 트래픽을 옮기므로 DNS 캐시로 인한 전환 지연 없음 |
| 각 환경은 Cloudflare Tunnel로 연결 | 앱 VM의 cloudflared가 Cloudflare로 바깥 방향 연결을 맺으므로 서버의 인바운드 포트와 클라우드별 공인 로드 밸런서가 불필요함. AWS·GCP·온프레미스를 같은 방식으로 연결 |
| 앱 서버 인바운드 전면 차단 | cloudflared(7844·443)와 Tailscale 모두 바깥 방향 연결만 사용. 외부에서 직접 공격할 포트가 없고, 접속자 IP가 바뀌어도 설정 변경 불필요 |
| AWS EC2는 퍼블릭 서브넷 | 프라이빗 서브넷이면 바깥 연결에 NAT Gateway(시간당 과금)가 필요함. 공인 IP를 붙이고 인바운드를 닫아 비용 없이 바깥 연결만 확보. GCP VM도 같은 이유로 외부 IP 사용, Cloud NAT 미사용 |
| 중앙 오케스트레이터 없음 | 파트마다 자기 서버(DB 서버, 캐시 서버, 트래픽 서버)를 두고 공통 API 형식으로 서로 호출. 트래픽 파트는 가중치 API만 제공 |

### 3.2 로드 밸런서 설정

| 항목 | 값 | 이유 |
| --- | --- | --- |
| 분배 방식(steering) | random | 요청마다 pool 가중치에 비례한 확률로 선택. 요청 단위로 나뉘어야 95:5 같은 카나리 비율이 바로 반영됨 |
| 가중치 | pool별 0~1 상대값 | 비율만 맞으면 됨(75:25 → 0.75:0.25). 퍼센트 변환은 트래픽 서버가 담당. 허용 단위는 실험(4.10)으로 확인 |
| 세션 어피니티 | 끔 | 세션이 Redis에 있어 어느 환경이 받아도 같음. 켜면 가중치를 바꿔도 기존 사용자가 옮겨지지 않음 |
| pool 간 장애 전환 | 켬, fallback pool 지정 | 한 pool이 unhealthy면 나머지로 전달 |
| 헬스체크(monitor) | HTTPS `GET /readyz`, 기대 코드 200 | 앱의 요청 수신 준비 여부 확인(3.3). 터널 엔드포인트에는 TCP monitor 사용 불가 |
| 재시도 | 기본 구성으로 시작 | Cloudflare는 오리진 연결 오류(521·522·523·525·526)일 때만 다른 pool로 한 번 재시도함. 앱이 돌려준 500·503은 그대로 사용자에게 전달됨. 기본 구성으로 실패 0건이 안 나오면 그때 Cloudflare Worker 재시도 추가 |
| Terraform으로 관리할 경우 | 가중치는 `ignore_changes` | 가중치는 트래픽 서버가 바꾸므로 `terraform apply`가 되돌리면 안 됨 |

### 3.3 `/readyz` 약속 (앱 파트와)

`/readyz`는 "이 인스턴스에 지금 요청을 보내도 되는가"만 응답.

- 기동 시 연결 결과와 종료 중 여부만 보고 200 또는 503 응답
- **요청마다 DB(PgBouncer)를 조회하지 않음**
  - 컷오버 중 PgBouncer가 PAUSE하면 쿼리가 대기함
  - 이때 `/readyz`가 DB를 보면 AWS·GCP 헬스체크가 동시에 실패해 모든 pool이 빠짐
  - DB는 두 환경이 같이 쓰므로 DB 상태로 환경을 고를 이유도 없음
- SIGTERM 수신 시 `/readyz`를 바로 503으로 바꾸고, 처리 중인 요청을 마친 뒤 종료
- 컨테이너 종료 유예 35초 이상

### 3.4 환경을 빼는 순서

1. **가중치 0**: 앱이 돌려준 503은 재시도되지 않으므로, 앱을 끄기 전에 새 요청부터 차단
2. **드레이닝**: 처리 중인 요청 완료 대기
3. **앱 종료**: SIGTERM, graceful shutdown
4. **cloudflared 종료**: 응답이 터널로 나가므로 앱보다 먼저 끄지 않음
5. **pool 제거**: 롤백 기간 이후 비활성화·삭제

### 3.5 터널과 로드 밸런서 연결

Cloudflare 문서([Public load balancers](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/public-load-balancers/))의 규칙:

- 터널에 published application route가 먼저 있어야 함
- pool 엔드포인트 주소는 `<터널 UUID>.cfargotunnel.com`, Host 헤더에 그 route의 호스트명 지정
- 앱 호스트명을 엔드포인트로 직접 넣을 수 없음
- 터널 엔드포인트에는 TCP monitor 사용 불가
- 한 pool에 같은 터널 UUID 중복 불가

호스트명 구성:

| 호스트명 | 역할 |
| --- | --- |
| `app.<도메인>` | 로드 밸런서 호스트명. 사용자는 항상 이 주소만 사용 |
| 환경별 호스트명 (실험 `exp-a`·`exp-b`, 운영 `aws`·`gcp`·`onprem`) | 각 터널의 route 호스트명. pool 엔드포인트의 Host 헤더로 사용 |

두 터널에 같은 `app.<도메인>` route를 넣지 않는 이유: route를 만들면 대시보드가 같은 이름의 DNS 레코드를 자동 생성해, 터널끼리 그리고 LB 호스트명과 겹침.

요청 경로:

1. 사용자 → `app.<도메인>`
2. LB가 가중치로 pool 선택
3. 그 pool의 엔드포인트 `<UUID>.cfargotunnel.com`으로 `Host: exp-b.<도메인>` 헤더와 함께 전달
4. 터널이 호스트명으로 route를 찾아 `http://localhost:8000`으로 전달

- API 응답은 캐시되지 않음 (`cf-cache-status: DYNAMIC`. Cloudflare 기본 캐시 대상은 정적 파일 확장자뿐)
- 실제 앱은 웹(`:3000`)과 API(`:8000`)가 나뉘므로 route에 `/api/*` → `:8000`, 나머지 → `:3000` 규칙 사용

### 3.6 사설망: Tailscale

| 연결 | 방식 |
| --- | --- |
| 각 환경 앱 → PgBouncer(6432)·Redis 프록시(6379) | Tailscale |
| 관리 VPC → RDS·Cloud SQL(5432), ElastiCache·Memorystore(6379) | Tailscale + 각 VPC의 서브넷 라우터 (관리형 서비스에는 Tailscale 설치 불가) |
| GCP Redis Sync Worker → ElastiCache | Tailscale + 서브넷 라우터 |
| 온프레미스 맥미니 | Tailscale에 직접 참여 (대역 광고 불필요) |
| RDS → Cloud SQL 논리 복제 | Tailscale 불가(관리형끼리 직접 연결). 공인+TLS 또는 VPN, 미정 |
| S3 → GCS | 사설망 불필요 (rclone이 양쪽 API를 HTTPS로 호출) |

- **서버는 사람 계정이 아니라 태그가 붙은 인증 키로 참여**
  - 태그: `tag:aws`, `tag:gcp`, `tag:mgmt`, `tag:onprem`
  - 사람 계정에 묶이면 그 사람이 빠질 때 서버도 같이 빠짐. ACL도 태그 단위로 작성 가능
- **서브넷 라우터는 앱 VM과 관리 호스트가 겸함.** 광고 대역:
  - AWS 앱 EC2 → `10.10.0.0/16`
  - 관리 호스트 → `10.30.0.0/16`
  - GCP 앱 GCE → `10.20.0.0/16`, `10.21.0.0/16`
- **라우터 요건** (인프라 파트에 요청): AWS는 Source/Destination Check 끔, GCP는 IP 포워딩 허용
- **Cloud SQL 경로**
  - Cloud SQL은 Google 관리 VPC에 있고 우리 VPC와 피어링으로 연결됨
  - 피어링은 한 다리 건너 전달되지 않으므로 관리 VPC → Cloud SQL은 GCP 서브넷 라우터의 SNAT로 해결
  - 실제 연결 여부는 운영 환경 구성(4.14)에서 반드시 확인

ACL 초안 (문법은 적용 시점의 Tailscale 문서로 확인):

- `tag:aws`·`tag:gcp`·`tag:onprem` → `tag:mgmt`의 6432, 6379
- `tag:mgmt` → 각 VPC 대역의 5432, 6379
- 관리자 → 전체 (운영용)
- `autoApprovers.routes`: `10.10.0.0/16` → `tag:aws`, `10.20.0.0/16`·`10.21.0.0/16` → `tag:gcp`, `10.30.0.0/16` → `tag:mgmt`

### 3.7 VPC 대역

Tailscale 서브넷 라우터로 모든 VPC가 하나의 라우팅 공간이 되므로 대역 중복 불가.

| 대역 | 용도 |
| --- | --- |
| `10.10.0.0/16` | AWS 앱 VPC |
| `10.20.0.0/16` | GCP 앱 VPC |
| `10.21.0.0/16` | GCP Cloud SQL 사설 연결 예약 대역 (Private Services Access) |
| `10.30.0.0/16` | AWS 관리 VPC |
| `10.40.0.0/16` 이후 | 예비 |

쓰지 않는 대역:

- `192.168.0.0/16`: 집 공유기, 온프레미스와 충돌
- `172.17.0.0/16`: Docker 기본 브리지
- `100.64.0.0/10`: Tailscale 주소

| VPC | 서브넷 | 가용 영역 | 용도 |
| --- | --- | --- | --- |
| AWS 앱 | `10.10.0.0/24` | 2a | 앱 EC2 (퍼블릭, 인바운드 없음, 서브넷 라우터 겸용) |
| AWS 앱 | `10.10.1.0/24` | 2c | 예비 |
| AWS 앱 | `10.10.10.0/24`, `10.10.11.0/24` | 2a, 2c | RDS, ElastiCache (서브넷 그룹에 AZ 2개 필요) |
| AWS 관리 | `10.30.0.0/24` | 2a | 관리 호스트 (퍼블릭, 인바운드 없음, 서브넷 라우터 겸용) |
| GCP 앱 | `10.20.0.0/24` | - | 앱 GCE (서브넷 라우터 겸용), Memorystore PSC 엔드포인트 |

### 3.8 도메인

도메인 하나를 서브도메인으로 분리.

| 주소 | 용도 |
| --- | --- |
| `app.<도메인>` | 사용자 앱, 로드 밸런서 대상 |
| `platform.<도메인>` | 이전 서비스 화면. 관리 호스트 터널로 직접 연결, Cloudflare Access로 로그인 제한 |
| `assets.<도메인>` | 객체 URL 고정용 (스토리지 파트) |

### 3.9 트래픽 서버 API

구현된 플랫폼 경로는 [빠른 시작](../README.md)과 [API 안내](api.md)를 따른다. 초기 설계의 drain과 telemetry는 후속 작업이다. 컨테이너·로컬 실행에서는 한 서버가 한 LB의 변경을 직렬로 처리하며 작업 이력을 SQLite에 보존한다.

### 3.10 다른 파트에 요청할 약속

| 대상 | 약속 | 이유 |
| --- | --- | --- |
| 앱 | `/readyz` 규칙 (3.3) | 컷오버 중 헬스체크 동시 실패 방지 |
| 앱 | SIGTERM 시 `/readyz` 503, 처리 중 요청 완료 후 종료, 종료 유예 35초 이상 | 드레이닝 중 요청 실패 방지 |
| 앱 | 모든 응답에 `X-Served-By: aws/gcp/onprem` 헤더 | 응답한 환경을 k6로 집계 |
| 앱 | 세션은 Redis에, 주문·공구 참여에 `Idempotency-Key`, API는 상대 경로 `/api/...` | 어느 환경이 받아도 같은 결과, 재시도해도 중복 주문 없음, 웹에 특정 환경 주소가 박히지 않음 |
| 앱 | HTTP·DB 드라이버·Redis 클라이언트 제한 시간 > 허용 전환 대기 | 컷오버 대기 중 타임아웃 실패 방지 |
| 인프라 | 각 앱 VM에 cloudflared 컨테이너, 인바운드 없음, 아웃바운드 7844·443 허용 | 3.1 |
| 인프라 | 터널 토큰·Tailscale 인증 키는 시크릿 참조 | 초기화 스크립트나 Terraform 변수에 비밀값이 들어가지 않게 |
| 인프라 | VPC마다 서브넷 라우터 VM 지정, AWS Source/Dest Check 끔, GCP IP 포워딩 허용 | 3.6 |

---

## 4. 구현 과정

전체 순서:

1. 준비 → Cloudflare 기본 → VPC
2. 실험 환경(testapp, 서버 2대, 터널, LB)에서 Cloudflare 동작 검증
3. Tailscale 사설망 → 트래픽 서버 → k6
4. 운영 환경 구성 → 무중단 리허설

### 4.1 준비

**로컬 도구**

- `aws` CLI v2, `session-manager-plugin` (EC2 접속용)
- `gcloud` CLI
- `cloudflared`, `k6`, Docker, Python 3.11+
- Tailscale (사설망 단계에서)

**CLI 설정**

```bash
aws login --profile foxtrot          # 임시 자격 증명, 최대 12시간. 만료되면 다시 실행
export AWS_PROFILE=foxtrot           # 이 터미널의 aws 명령이 모두 foxtrot 프로필 사용
aws configure set region ap-northeast-2 --profile foxtrot

gcloud config set project <GCP 프로젝트 ID>
gcloud config set compute/region asia-northeast3
gcloud config set compute/zone asia-northeast3-a
```

**필요 권한**

| 대상 | 권한 | 비고 |
| --- | --- | --- |
| AWS | VPC·서브넷·라우팅·보안 그룹·EC2 생성 | |
| AWS | `foxtrot-*` IAM 역할·인스턴스 프로파일 생성, 정책 연결, `iam:PassRole`, `ssm:StartSession` | EC2 Session Manager 접속용 |
| GCP | `roles/editor` | 리소스 생성 |
| GCP | `roles/iap.tunnelResourceAccessor` | IAP 접속용. `roles/editor`에 포함되지 않음 |
| Cloudflare | zone 관리, 결제 수단 등록 | Load Balancing 애드온이 유료 |

**태그 규칙**

- AWS: `Project=foxtrot`, `Owner=traffic`, `Purpose=<용도>` (+ `Name`)
- GCP 라벨: `project=foxtrot`, `owner=traffic`, `purpose=<용도>` (소문자만 허용)
- 다른 프로젝트와 같이 쓰는 계정이므로 이 태그로 우리 리소스를 구분

### 4.2 Cloudflare 기본: zone과 API 토큰

1. 도메인을 Cloudflare zone으로 추가하고 상태 Active 확인
2. API 토큰 2개 발급 (My Profile → API Tokens → Create Token → Custom)

    | 토큰 | 용도 | 권한 | `.env` 키 |
    | --- | --- | --- | --- |
    | 자동화용 | 실험·설정 | Load Balancers 수정(zone), Load Balancing Monitors and Pools 수정(계정), Cloudflare Tunnel 수정(계정), Analytics 읽기 | `CF_API_TOKEN_AUTOMATION` |
    | 트래픽 서버용 | 운영 | Load Balancers 수정, Monitors and Pools 읽기, Analytics 읽기 (Tunnel 권한 없음) | `CF_API_TOKEN` |

    - 트래픽 서버는 random steering의 pool 가중치만 바꾸면 됨. 가중치는 LB 설정의 일부라 "Load Balancers 수정"으로 충분
    - 서버 토큰이 유출돼도 터널·pool 생성이나 변경 불가
    - 만료일 지정 (해커톤 종료 이후 적당한 날짜)
3. 토큰 검증, zone ID·account ID를 `.env`에 기록 (`CF_ZONE_ID`, `CF_ACCOUNT_ID`)

    ```bash
    set -a; . ../.env; set +a   # traffic/ 폴더 기준, 레포 바깥 .env
    curl -s -H "Authorization: Bearer $CF_API_TOKEN_AUTOMATION" \
      https://api.cloudflare.com/client/v4/user/tokens/verify | grep -o '"status":"[a-z]*"'
    ```

    - `"status":"active"`면 정상
    - 계정 단위 목록 API(터널 등)는 권한이 없어도 `200`과 빈 목록을 반환하므로, 권한 확인을 목록 결과로 판단하지 않음

### 4.3 AWS 앱 VPC (서울 `ap-northeast-2`)

실제 이전에도 쓰는 고정 환경. 같은 터미널에서 순서대로 실행 (앞 명령의 ID를 변수로 이어 씀).

```bash
TAGS='{Key=Project,Value=foxtrot},{Key=Owner,Value=traffic},{Key=Purpose,Value=aws-app-vpc}'

# VPC, DNS 호스트 이름
VPC_ID=$(aws ec2 create-vpc --cidr-block 10.10.0.0/16 \
  --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=foxtrot-aws-app},$TAGS]" \
  --query 'Vpc.VpcId' --output text)
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames '{"Value":true}'

# 서브넷 4개
subnet() {  # 이름 대역 가용영역
  aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block $2 --availability-zone $3 \
    --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=foxtrot-aws-app-$1},$TAGS]" \
    --query 'Subnet.SubnetId' --output text
}
SUBNET_PUB_2A=$(subnet public-2a 10.10.0.0/24 ap-northeast-2a)
SUBNET_PUB_2C=$(subnet public-2c 10.10.1.0/24 ap-northeast-2c)
SUBNET_DATA_2A=$(subnet data-2a 10.10.10.0/24 ap-northeast-2a)
SUBNET_DATA_2C=$(subnet data-2c 10.10.11.0/24 ap-northeast-2c)
aws ec2 modify-subnet-attribute --subnet-id $SUBNET_PUB_2A --map-public-ip-on-launch
aws ec2 modify-subnet-attribute --subnet-id $SUBNET_PUB_2C --map-public-ip-on-launch

# 인터넷 게이트웨이
IGW_ID=$(aws ec2 create-internet-gateway \
  --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=foxtrot-aws-app-igw},$TAGS]" \
  --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --internet-gateway-id $IGW_ID --vpc-id $VPC_ID

# 퍼블릭 라우팅 테이블: 0.0.0.0/0 → IGW
RTB_PUBLIC=$(aws ec2 create-route-table --vpc-id $VPC_ID \
  --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=foxtrot-aws-app-public},$TAGS]" \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id $RTB_PUBLIC --destination-cidr-block 0.0.0.0/0 --gateway-id $IGW_ID
aws ec2 associate-route-table --route-table-id $RTB_PUBLIC --subnet-id $SUBNET_PUB_2A
aws ec2 associate-route-table --route-table-id $RTB_PUBLIC --subnet-id $SUBNET_PUB_2C

# 데이터 라우팅 테이블: VPC 내부 경로만
RTB_DATA=$(aws ec2 create-route-table --vpc-id $VPC_ID \
  --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=foxtrot-aws-app-data},$TAGS]" \
  --query 'RouteTable.RouteTableId' --output text)
aws ec2 associate-route-table --route-table-id $RTB_DATA --subnet-id $SUBNET_DATA_2A
aws ec2 associate-route-table --route-table-id $RTB_DATA --subnet-id $SUBNET_DATA_2C

# 기본(main) 라우팅 테이블에 이름만 붙임 (경로 추가 없음)
RTB_MAIN=$(aws ec2 describe-route-tables --filters Name=vpc-id,Values=$VPC_ID Name=association.main,Values=true \
  --query 'RouteTables[0].RouteTableId' --output text)
aws ec2 create-tags --resources $RTB_MAIN \
  --tags Key=Name,Value=foxtrot-aws-app-main Key=Project,Value=foxtrot Key=Owner,Value=traffic Key=Purpose,Value=aws-app-vpc

# 인바운드 없는 보안 그룹 (아웃바운드는 기본값인 전체 허용)
SG_NOINBOUND=$(aws ec2 create-security-group --vpc-id $VPC_ID \
  --group-name foxtrot-aws-app-noinbound \
  --description "No inbound. Outbound only for cloudflared, Tailscale, package installs" \
  --tag-specifications "ResourceType=security-group,Tags=[{Key=Name,Value=foxtrot-aws-app-noinbound},$TAGS]" \
  --query 'GroupId' --output text)
```

| 구성 | 이유 |
| --- | --- |
| public 서브넷에 공인 IP 자동 할당 | NAT Gateway 없이 바깥 연결 (3.1) |
| data 서브넷은 인터넷 경로 없음 | RDS·ElastiCache는 인터넷 불필요. 서브넷 그룹에 AZ 2개 필요해서 2a·2c |
| main 라우팅 테이블에 경로 추가 안 함 | 연결 없이 만든 서브넷이 실수로 인터넷에 열리지 않게 |
| 별도 보안 그룹 `foxtrot-aws-app-noinbound` | VPC 기본 보안 그룹 `default`는 같은 그룹끼리 인바운드를 허용하므로 사용하지 않음 |

### 4.4 GCP 앱 VPC (서울 `asia-northeast3`)

```bash
gcloud compute networks create foxtrot-gcp-app --subnet-mode=custom \
  --description="Foxtrot GCP app VPC. Project=foxtrot Owner=traffic"

gcloud compute networks subnets create foxtrot-gcp-app-asia-ne3 \
  --network=foxtrot-gcp-app --region=asia-northeast3 --range=10.20.0.0/24 \
  --enable-private-ip-google-access \
  --description="App GCE, subnet router, Memorystore PSC endpoint"
```

| 구성 | 이유 |
| --- | --- |
| custom 모드 | auto 모드는 모든 리전에 서브넷을 자동 생성해 대역 통제 불가 |
| Private Google Access 켬 | 외부 IP 없는 VM도 Google API(GCS 등) 사용 가능 |
| 방화벽 규칙 없음 | GCP 기본 동작이 들어오는 연결 차단, 나가는 연결 허용. IAP 접속용 규칙만 4.7에서 추가 |

- Cloud SQL 예약 대역 `10.21.0.0/16`은 Cloud SQL 생성 시 DB 파트와 함께 설정
- Compute Engine API를 켜면 `default` 네트워크가 자동 생성됨. SSH·RDP·ICMP를 인터넷 전체에 여는 기본 방화벽 규칙이 있으므로 사용하지 않고, 팀과 삭제 논의

### 4.5 testapp과 VM 초기화 스크립트

실제 공구 앱 대신 Cloudflare 동작만 확인하는 작은 FastAPI 앱. 상세는 [testapp/README.md](../testapp/README.md).

- **따로 만든 이유**: DB·Redis·스토리지가 없어, 실험 중 실패가 나면 트래픽 계층 문제로 바로 판단 가능
- 트래픽 계층이 기대는 동작은 실제 앱과 동일하게 구현
  - 모든 응답에 `X-Served-By`
  - `/readyz`를 파일로 503 전환
  - SIGTERM 드레이닝 (`/readyz` 503 → 일정 시간 요청 계속 처리 → 종료)
  - `/slow`로 긴 요청 재현
- 컨테이너는 `127.0.0.1:8000`에만 열어 같은 서버의 cloudflared만 접근, `--stop-timeout 35`로 실행

로컬 확인:

```bash
docker build -t foxtrot-testapp ./testapp
docker run -d --name testapp -p 127.0.0.1:8000:8000 -e SERVED_BY=local --stop-timeout 35 foxtrot-testapp
curl -si http://127.0.0.1:8000/readyz | grep -iE '^HTTP|x-served-by'
```

VM 초기화 스크립트 생성: `testapp/make-init.sh aws|gcp|onprem`. Docker 설치 → testapp 빌드·실행 스크립트를 출력하고, 이 출력을 AWS `user_data`, GCP `startup-script`로 전달.

| 설계 | 이유 |
| --- | --- |
| testapp 파일을 스크립트 안에 포함 | 레포가 비공개라 서버에서 clone 불가. 비밀값은 넣지 않음 |
| 재실행해도 안전하게 작성 | GCP는 부팅마다 startup-script 재실행 (AWS `user_data`는 첫 부팅에 한 번) |
| apt 잠금 대기(`DPkg::Lock::Timeout=600`) | Ubuntu 첫 부팅 직후 unattended-upgrades가 apt 잠금을 잡고 있어 바로 설치하면 실패 |
| 마지막에 `testapp-check: readyz=<코드> x-served-by=<환경>`을 부팅 로그에 기록 | 셸 접속 없이 결과 확인 가능 |

### 4.6 실험 서버 두 대

실제 이전 경로와 같게 AWS 1대(환경 A), GCP 1대(환경 B)로 Cloudflare 동작 실험.

**환경 A: EC2**

```bash
testapp/make-init.sh aws > init-aws.sh
AMI=$(aws ec2 describe-images --owners 099720109477 \
  --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*' 'Name=state,Values=available' \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)
TAGS='{Key=Name,Value=foxtrot-traffic-test-a},{Key=Project,Value=foxtrot},{Key=Owner,Value=traffic},{Key=Purpose,Value=traffic-test-a}'

aws ec2 run-instances --image-id "$AMI" --instance-type t4g.small \
  --subnet-id $SUBNET_PUB_2A --security-group-ids $SG_NOINBOUND \
  --credit-specification CpuCredits=standard \
  --metadata-options HttpTokens=required,HttpEndpoint=enabled \
  --user-data file://init-aws.sh \
  --tag-specifications "ResourceType=instance,Tags=[$TAGS]" "ResourceType=volume,Tags=[$TAGS]" "ResourceType=network-interface,Tags=[$TAGS]" \
  --query 'Instances[0].[InstanceId,PrivateIpAddress,State.Name]' --output text

# 약 3분 뒤 부팅 로그 확인
aws ec2 get-console-output --instance-id <인스턴스 ID> --latest --output text | grep testapp-check
```

**환경 B: GCE**

```bash
testapp/make-init.sh gcp > init-gcp.sh

gcloud compute instances create foxtrot-traffic-test-b \
  --zone=asia-northeast3-a --machine-type=e2-small \
  --subnet=foxtrot-gcp-app-asia-ne3 \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=10GB --boot-disk-type=pd-balanced \
  --no-service-account --no-scopes \
  --tags=traffic-test \
  --labels=project=foxtrot,owner=traffic,purpose=traffic-test-b \
  --metadata=block-project-ssh-keys=TRUE \
  --metadata-from-file=startup-script=init-gcp.sh

# 약 1~2분 뒤 시리얼 출력 확인
gcloud compute instances get-serial-port-output foxtrot-traffic-test-b --zone=asia-northeast3-a | grep testapp-check
```

두 환경 모두 `testapp-check: readyz=200 x-served-by=<aws|gcp>`가 보이면 완료.

| | 환경 A | 환경 B | 이유 |
| --- | --- | --- | --- |
| 사양 | t4g.small (arm64, 2 vCPU·2GB), gp3 8GB | e2-small (x86), pd-balanced 10GB | 가장 싼 소형. 서울 GCP에는 ARM 머신이 없어 x86 |
| 비용 | 하루 약 $0.65 | 하루 약 $0.68 | 쓰지 않을 때 중지·삭제 |
| CPU 크레딧 | standard | - | unlimited 추가 과금 방지 |
| 메타데이터 | IMDSv2 필수 | `block-project-ssh-keys=TRUE` | IMDS 토큰 탈취 방지. gcloud가 SSH 키를 프로젝트 전체가 아닌 이 VM에만 등록 |
| 권한 | IAM 역할 없음 (4.7에서 연결) | 서비스 계정 없음 | 실험 서버는 클라우드 API를 호출하지 않음 |
| 부팅 → `readyz=200` | 약 170초 | 약 75초 | 확인 대기 시간 기준 |

### 4.7 서버 접속

원칙: **접속자 IP를 허용하는 SSH 인바운드는 쓰지 않음.** 장소마다 IP가 바뀌어 매번 규칙 수정이 필요하기 때문. 대신 클라우드가 제공하는 접속 경로 사용.

**GCE: IAP TCP 포워딩**

```bash
# Google IAP 대역의 22번만, 태그 traffic-test VM에만 허용
gcloud compute firewall-rules create foxtrot-gcp-app-allow-iap-ssh \
  --network=foxtrot-gcp-app --direction=INGRESS --action=ALLOW --rules=tcp:22 \
  --source-ranges=35.235.240.0/20 --target-tags=traffic-test \
  --description="SSH from IAP TCP forwarding to traffic test VMs only"

gcloud compute ssh foxtrot-traffic-test-b --zone=asia-northeast3-a --tunnel-through-iap
```

- `roles/iap.tunnelResourceAccessor` 필요
- IAP API를 켜지 않아도 동작

**EC2: Session Manager (미검증, IAM 역할 생성 권한 필요)**

```bash
aws iam create-role --role-name foxtrot-traffic-ec2 \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
  --tags Key=Project,Value=foxtrot Key=Owner,Value=traffic Key=Purpose,Value=traffic-test-ssm
aws iam attach-role-policy --role-name foxtrot-traffic-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name foxtrot-traffic-ec2
aws iam add-role-to-instance-profile --instance-profile-name foxtrot-traffic-ec2 --role-name foxtrot-traffic-ec2
aws ec2 associate-iam-instance-profile --instance-id <인스턴스 ID> \
  --iam-instance-profile Name=foxtrot-traffic-ec2
aws ssm describe-instance-information   # 몇 분 뒤 인스턴스가 보이면 등록 완료
aws ssm start-session --target <인스턴스 ID>
```

- SSM 에이전트는 Ubuntu 이미지에 기본 포함, 맥에는 `session-manager-plugin` 필요
- 콘솔의 역할 생성 화면은 `iam:ListPolicies`가 없으면 정책 선택 단계에서 막힘. CLI는 해당 권한 불필요
- 대안: 계정 단위 SSM 기본 호스트 관리 설정(Default Host Management Configuration)
  - 켜면 인스턴스 프로파일 없이도 등록됨
  - EC2 부팅 로그의 "Systems Manager's instance management role is not configured for account"는 이 설정이 꺼져 있다는 뜻
  - 계정 전체 설정이라 계정 관리자 판단 필요
- EC2 Instance Connect Endpoint는 서비스 연결 역할(`AWSServiceRoleForEC2InstanceConnect`)이 필요해 사용하지 않음

### 4.8 Cloudflare Tunnel

환경마다 터널 하나 (실험 `exp-a`·`exp-b`, 운영 `aws`·`gcp`·`onprem`·`platform`). 아래는 `exp-b`(GCE) 기준.

**원격 관리형을 쓰는 이유**: 서버에는 토큰만 두고 route 등 설정은 대시보드·API에서 변경. 서버에 설정 파일 불필요.

1. Cloudflare **메인 대시보드**(Zero Trust 화면 아님) → Networking → Tunnels → Create a tunnel → 이름 `exp-b` → 환경 Docker
2. 토큰을 화면에 띄우지 않고 클립보드에서 레포 바깥 `.env`로 바로 저장
    - 아래 명령을 터미널에 붙여 넣기만 한 상태에서, 대시보드 Docker 명령 옆 복사 버튼 클릭 → Enter
    - 명령을 복사하는 순간 클립보드가 덮이므로 순서 중요

    ```bash
    # traffic/ 폴더에서 실행. 클립보드의 docker 명령에서 토큰(eyJ...)만 골라 저장하고 클립보드를 비움
    printf 'CF_TUNNEL_TOKEN_EXP_B=%s\n' "$(pbpaste | grep -oE 'eyJ[A-Za-z0-9+/=_-]+' | head -1)" >> ../.env && pbcopy < /dev/null
    ```

    - `head -1`이 없으면 토큰이 두 번 잡혀 중복 저장됨
    - 저장 값은 base64 JSON, 키 `a`(계정 ID), `t`(터널 ID), `s`(비밀) 포함
3. 토큰을 서버 파일로 전송
    - 파일 소유자는 cloudflared 컨테이너 사용자(uid 65532), 권한 400
    - GCE: 맥 `.env`에서 읽어 IAP 연결의 표준입력으로 넘김. 화면·명령줄·초기화 스크립트에 남지 않음

    ```bash
    grep '^CF_TUNNEL_TOKEN_EXP_B=' ../.env | cut -d= -f2- | tr -d '\n' | \
      gcloud compute ssh foxtrot-traffic-test-b --zone=asia-northeast3-a --tunnel-through-iap \
      --command='sudo install -d -m 755 /etc/cloudflared && sudo sh -c "umask 077 && cat > /etc/cloudflared/exp-b.token" && sudo chown 65532:65532 /etc/cloudflared/exp-b.token && sudo chmod 400 /etc/cloudflared/exp-b.token && sudo ls -l /etc/cloudflared/exp-b.token'
    ```

    - EC2 (미검증): `start-session` 셸에서 화면에 보이지 않게 붙여 넣음. `aws ssm send-command`는 명령 파라미터가 SSM 실행 기록에 남으므로 사용하지 않음

    ```bash
    sudo install -d -m 755 /etc/cloudflared
    read -rs T   # 토큰을 붙여 넣고 Enter (화면에 표시되지 않음)
    printf %s "$T" | sudo sh -c 'umask 077 && cat > /etc/cloudflared/exp-a.token'; unset T
    sudo chown 65532:65532 /etc/cloudflared/exp-a.token && sudo chmod 400 /etc/cloudflared/exp-a.token
    ```

4. 서버에서 cloudflared 실행

    ```bash
    sudo docker run -d --name cloudflared --restart unless-stopped --network host --stop-timeout 35 \
      -v /etc/cloudflared/exp-b.token:/etc/cloudflared/token:ro \
      cloudflare/cloudflared:2026.10.7 tunnel --no-autoupdate run --token-file /etc/cloudflared/token
    ```

    | 옵션 | 이유 |
    | --- | --- |
    | `--network host` | 서버의 `127.0.0.1:8000`(testapp)에 접근 |
    | `--stop-timeout 35` | cloudflared는 종료 시 처리 중인 요청을 최대 30초 기다리는데, Docker 기본 유예는 10초라 도중에 강제 종료됨 |
    | 버전 고정, `--no-autoupdate` | 버전은 이미지 태그로 관리 |
    | `--token-file` (cloudflared 2025.4.0+) | 토큰이 명령줄·프로세스 목록에 노출되지 않음 |

5. 대시보드에서 터널 상태 Healthy 확인 (서울 데이터센터 여러 곳에 연결)
6. 터널 상세 → Routes → Add route → Published application
    - 호스트명 `exp-b.<도메인>`, Service URL `http://localhost:8000`
    - DNS CNAME 자동 생성
7. 외부에서 확인: `curl -si https://exp-b.<도메인>/readyz | grep -iE '^HTTP|x-served-by'` → 200, `x-served-by: gcp`

### 4.9 Load Balancer

**1. Load Balancing 애드온 활성화 (유료)**

대시보드 Load Balancing → Enable Load Balancing → 옵션 선택 → 결제. 공식 문서에 세부 가격표가 없어 활성화 화면에서 확인.

| 항목 | 내용 (검색 기준, 화면에서 재확인) |
| --- | --- |
| 기본 | 월 $5, 엔드포인트 2개 포함 (실험 `exp-a`·`exp-b`에 맞음) |
| 엔드포인트 추가 | 1개당 월 $5 (운영 aws·gcp·onprem 3개면 월 $10) |
| 헬스체크 간격 | 60초 포함. 30초 +$10/월, 15초 +$15/월 |
| 헬스체크 지역 | 1곳 포함 |
| DNS 조회 | LB 호스트명 첫 50만 건 무료, 이후 50만 건당 $0.50 |

- 헬스체크 60초 간격이면 앱이 갑자기 죽었을 때 pool에서 빠지기까지 1분 넘게 걸릴 수 있음. 계획된 이전에서는 "가중치 먼저 0"(3.4) 순서 유지
- 해커톤 종료 후 해지

**2. monitor, pool, LB 생성**

| 대상 | 설정 |
| --- | --- |
| monitor | HTTPS, `GET /readyz`, 기대 코드 200. 허용되는 최소 간격 기록 |
| pool `exp-a` | 엔드포인트 `<exp-a 터널 UUID>.cfargotunnel.com`, Host 헤더 `exp-a.<도메인>`, monitor 연결 |
| pool `exp-b` | 엔드포인트 `<exp-b 터널 UUID>.cfargotunnel.com`, Host 헤더 `exp-b.<도메인>`, monitor 연결 |
| LB | 호스트명 `app.<도메인>`, steering random, 세션 어피니티 없음, failover across pools 켬, fallback pool `exp-a`, 가중치 a=1·b=0 |

- 터널 UUID는 대시보드 터널 목록 또는 `.env` 토큰의 `t` 값
- 확인: `curl -sI https://app.<도메인>/ | grep -i x-served-by` 결과가 `aws`면 정상

### 4.10 Cloudflare 동작 실험

결과는 `docs/experiments.md`에 기록.

| # | 실험 | 방법 | 확인할 것 |
| --- | --- | --- | --- |
| 1 | 가중치 반영 시간 | API로 95:5 → 50:50 → 0:100, 반복 요청의 `X-Served-By` 관찰 | 비율이 바뀌기까지 걸리는 시간. 트래픽 서버의 작업 완료 판단 기준 |
| 2 | 비율 정확도 | k6 초당 50건 1분, 환경별 집계 | random steering 오차 |
| 3 | 가중치 단위 | 0.25 같은 값 설정 후 조회 | 반올림 여부 → 트래픽 서버 변환 규칙 확정 |
| 4 | 계획된 빼기 | 50:50에서 B를 3.4 순서로 제거, k6 동시 실행 | **실패 0건** 여부 |
| 5 | 갑작스러운 장애 | 50:50에서 B 앱 컨테이너 강제 종료 | 실패 건수, pool이 빠지기까지 시간. **터널 뒤 장애가 즉시 장애 전환(521~526) 대상으로 처리되는지가 핵심** |
| 6 | `/readyz` 503 | `docker exec testapp touch /tmp/testapp-not-ready` | pool에서 빠지는 시간 |
| 7 | 직접 주소 차단 | 환경별 DNS 레코드(`exp-b.<도메인>`)를 지우고 LB로 접속 | LB 경로가 그대로 동작하면 운영에서 직접 주소 우회 차단 가능 |
| 8 | Host 헤더 | testapp `GET /`의 `host` 필드 | 앱이 받는 Host |
| 9 | monitor 범위 | 앱만 503일 때 pool 상태 | monitor가 `/readyz`까지 보는지, cloudflared까지만 보는지 |

- 판단 기준
  - 4번 실패 0건 → 기본 구성 유지
  - 5번 실패가 크면 Worker 재시도 도입 여부를 문서로 정리
- 요청이 서울이 아닌 데이터센터(예: 홍콩)를 경유할 수 있음. 지연 측정 시 `cf-ray` 헤더 끝의 데이터센터 코드 확인

### 4.11 Tailscale 사설망

1. 관리 콘솔에서 MagicDNS 상태 확인
2. 3.6 ACL 초안을 `tailscale/policy.hujson`으로 작성해 콘솔 Access controls에 적용. `tagOwners`에 네 태그, 소유자는 관리자
3. 태그별 인증 키 발급 (재사용 가능, 사전 승인, 태그 지정). `.env`에 보관, 인프라 파트에는 시크릿으로 전달
4. 실험 서버에 설치 후 참여: `sudo tailscale up --authkey=<KEY> --advertise-tags=tag:aws` (B는 `tag:gcp`)
    - 키는 명령줄 대신 `--authkey=file:<경로>`로 넘기는 방식 검토
5. 서로 `tailscale ping`, ACL로 허용한 포트만 열리는지 `nc -zv`로 확인
6. 서브넷 라우터 시험 (환경 A)
    - `net.ipv4.ip_forward=1`
    - `--advertise-routes=10.10.0.0/16`
    - EC2 Source/Destination Check 끄기
    - B에서 A VPC의 다른 사설 주소로 접속되는지 확인

### 4.12 트래픽 서버 (`server/`)

1. FastAPI 프로젝트 생성. 설정은 환경변수로 받음
    - `CF_API_TOKEN`, `CF_ZONE_ID`, `CF_ACCOUNT_ID`, `CF_LB_ID`
    - pool 이름 ↔ ID 매핑
2. Cloudflare 클라이언트 작성
    - LB 조회, random steering pool 가중치 변경, pool 헬스 조회
    - 퍼센트 → 가중치 변환 함수와 단위 테스트
3. 3.9 API 구현. 작업은 백그라운드 실행, 이력은 SQLite
4. `drain`, 변경 이력, `rollback` 구현
5. 실험 환경 대상 통합 테스트, Dockerfile 작성
6. 공통 API 형식을 `docs/api.md`로 정리해 팀에 공유

### 4.13 k6 (`k6/`)

1. 초당 50건 constant-arrival-rate 시나리오
2. 지표: `X-Served-By`별 카운터, 실패율, p95. 단계 전환 시점은 태그나 로그로 표시
3. 결과 JSON 저장, 요약 표 생성. 4.10 실험을 이 스크립트로 재현 가능해야 함

### 4.14 운영 환경 구성

1. 인프라 파트에 전달
    - 3.7 대역·서브넷 표
    - 서브넷 라우터 요건
    - Tailscale 인증 키·터널 토큰 (시크릿)
2. AWS 관리 VPC `10.30.0.0/16`과 관리 호스트 준비 (구성은 4.3과 같은 방식)
3. 관리 호스트, AWS 앱 EC2, GCP 앱 GCE의 태그 참여 확인, 서브넷 경로 승인
4. 연결 검증
    - 앱 VM → 관리 호스트 6432·6379
    - 관리 호스트 → RDS·ElastiCache·Cloud SQL·Memorystore 사설 주소
    - Cloud SQL은 GCP 서브넷 라우터 SNAT 경유로 닿는지 반드시 확인
5. 운영 터널(`aws`, `gcp`, `onprem`, `platform`) 생성, pool·monitor·LB를 운영 구성으로 전환, 실험 pool·서버 정리
6. `platform.<도메인>`에 Cloudflare Access 정책 적용 (팀 이메일만)
7. 트래픽 서버를 관리 호스트에 배포

### 4.15 무중단 리허설

1. k6 초당 50건을 유지한 채 2장 흐름 전체 실행. 다른 파트 서버와는 API로 연동
2. 실패 0건 여부, 단계별 p95, 컷오버 구간 지연을 따로 집계
3. 환경 빼기 절차(3.4) 리허설
4. 결과를 데모 자료로 정리

### 4.16 (나중) 텔레메트리

- Cloudflare Analytics API로 pool별 요청 수·오류·지연 수집, `GET /telemetry`로 제공
- 수집 항목과 저장 위치는 AI 사양 추천 담당과 결정

### 4.17 정리 (해커톤 종료 후)

- Load Balancing 애드온 해지
- 실험 서버 2대 삭제
- 터널과 환경별 DNS 레코드 삭제
- 방화벽 `foxtrot-gcp-app-allow-iap-ssh` 삭제
- Cloudflare API 토큰, Tailscale 인증 키 폐기

---

## 5. 결정되지 않은 사항

- 이전 시나리오에 온프레미스 포함 여부 (온프레미스 → AWS → GCP, 또는 AWS → GCP만)
- 컷오버 후 DB 역이전 여부 (안 하면 롤백은 트래픽만)
- RDS → Cloud SQL 복제 경로: 공인 엔드포인트 + TLS, 또는 클라우드 VPN
- 사설망 방식: 전부 Tailscale, 또는 클라우드 간은 VPN·온프레미스만 Tailscale (현재 기본: 전부 Tailscale)
- `db-proxy.internal`, `redis-proxy.internal` 이름 해석 방식 (예: 초기화 스크립트로 `/etc/hosts`에 관리 호스트 Tailscale IP 등록). DB·캐시 파트와 합의 필요
- 가중치 단계별 관찰 시간, 다음 단계로 넘어가는 기준(실패율·p95), 판단 주체
- 허용 전환 대기 시간과 앱 제한 시간 값 (DB·캐시 파트와)
- 공통 API 형식 최종안 (팀 전체)
- 텔레메트리 수집 항목과 저장 위치

---

## 6. 주의할 점

**Cloudflare**

- 계정 단위 목록 API(터널, Workers 등)는 권한이 없어도 403 대신 `200`과 빈 목록 반환. zone 단위 API(DNS 등)는 `403` 반환
- 원격 관리형 터널은 메인 대시보드 Networking → Tunnels에 있음. route는 터널 상세 → Routes 탭
- 대시보드 메뉴는 자주 바뀌므로 막히면 공식 문서로 위치 확인

**AWS**

- 다른 프로젝트와 함께 쓰는 계정. `Project=foxtrot` 태그가 붙은 리소스만 다룸
- 비용 할당 태그 `Project`가 Inactive면 foxtrot만 거르는 예산 알림 생성 불가. 태그 활성화는 결제 설정 권한 필요. 그 전까지 실험 리소스는 사용 후 바로 중지·삭제
- 서울 리전에 기본 VPC 없음
- `user_data`는 첫 부팅에만 실행. 결과는 `aws ec2 get-console-output --latest`로 확인

**GCP**

- `roles/editor`에는 IAP 터널 권한(`iap.tunnelInstances.accessViaIAP`) 없음
- startup-script는 부팅마다 실행. 결과는 `gcloud compute instances get-serial-port-output`으로 확인
- Cloud Billing API가 꺼져 있어도 프로젝트를 지정하지 않고 호출하면 과금 카탈로그 조회 가능

**공통**

- `docker stop` 기본 유예는 10초라 드레이닝 도중 강제 종료됨. 앱과 cloudflared 모두 `--stop-timeout 35`로 실행
- 맥(arm64), EC2 t4g(arm64), GCE e2(x86), 맥미니(arm64) 혼재. 실제 앱 이미지는 amd64·arm64 둘 다 빌드 필요
