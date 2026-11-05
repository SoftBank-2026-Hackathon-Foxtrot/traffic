"""Cloudflare LB 조회와 pool 가중치 변경만 담당한다."""
import json
import os

import httpx


class Cloudflare:
    def __init__(self):
        names = ("CF_API_TOKEN", "CF_ZONE_ID", "CF_ACCOUNT_ID", "CF_LB_ID", "CF_POOL_IDS")
        missing = [name for name in names if not os.getenv(name)]
        if missing:
            raise ValueError("설정 필요: " + ", ".join(missing))
        self.token = os.environ["CF_API_TOKEN"]
        self.account = os.environ["CF_ACCOUNT_ID"]
        self.lb_path = f"/zones/{os.environ['CF_ZONE_ID']}/load_balancers/{os.environ['CF_LB_ID']}"
        try:
            self.pools = json.loads(os.environ["CF_POOL_IDS"])
        except json.JSONDecodeError:
            raise ValueError("CF_POOL_IDS는 환경 이름과 pool ID를 매핑한 JSON이어야 합니다.") from None
        if not isinstance(self.pools, dict) or not self.pools or not all(
            isinstance(name, str) and name and isinstance(pool, str) and pool
            for name, pool in self.pools.items()
        ) or len(set(self.pools.values())) != len(self.pools):
            raise ValueError("CF_POOL_IDS에는 중복 없는 pool ID를 지정하세요.")

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(
            base_url="https://api.cloudflare.com/client/v4",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=15,
        ) as client:
            response = await client.request(method, path, **kwargs)
        if response.is_error:
            raise RuntimeError(f"Cloudflare HTTP {response.status_code}")
        body = response.json()
        if not body.get("success"):
            raise RuntimeError("Cloudflare 요청 실패")
        return body["result"]

    async def get_lb(self):
        return await self.request("GET", self.lb_path)

    def weights(self, lb):
        if lb.get("enabled") is False:
            raise ValueError("LB가 비활성 상태입니다.")
        if any(lb.get(key) for key in ("pool_sets", "rules", "region_pools", "country_pools", "pop_pools")):
            raise ValueError("별도 라우팅 규칙이 없는 단순 LB 구성만 지원합니다.")
        if lb.get("steering_policy") != "random" or not lb.get("proxied"):
            raise ValueError("LB를 proxied=true, steering_policy=random으로 설정하세요.")
        if lb.get("session_affinity", "none") != "none":
            raise ValueError("LB의 session_affinity를 none으로 설정하세요.")
        if set(lb["default_pools"]) != set(self.pools.values()):
            raise ValueError("CF_POOL_IDS는 LB default_pools 전체와 일치해야 합니다.")
        steering = lb.get("random_steering") or {}
        configured = steering.get("pool_weights") or {}
        weights = {
            name: configured.get(pool, steering.get("default_weight", 1))
            for name, pool in self.pools.items()
        }
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("현재 LB 가중치 합계가 0입니다.")
        return {name: value / total * 100 for name, value in weights.items()}

    async def set_weights(self, weights):
        return await self.request("PATCH", self.lb_path, json={
            "random_steering": {
                "default_weight": 0,
                "pool_weights": {self.pools[name]: value / 100 for name, value in weights.items()},
            },
        })

    async def health(self):
        return {
            name: await self.request("GET", f"/accounts/{self.account}/load_balancers/pools/{pool}/health")
            for name, pool in self.pools.items()
        }
