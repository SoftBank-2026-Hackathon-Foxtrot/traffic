"""Run with: python -m unittest discover -s tests -v

Exercises the real FastAPI routes, SQLite and Cloudflare client; only HTTP to
Cloudflare is replaced. No credentials or external resources are used.
"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from server import main


class TrafficAPITest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch.object(main, "DB_PATH", Path(self.tmp.name) / "traffic.db"))
        self.enterContext(patch.dict(os.environ, {
            "TRAFFIC_SERVICE_TOKEN": "demo-secret",
            "CF_API_TOKEN": "test-token", "CF_ACCOUNT_ID": "test-account",
            "CF_ZONE_ID": "test-zone", "CF_LB_ID": "test-lb",
            "CF_POOL_IDS": '{"aws":"pool-a","gcp":"pool-b"}',
        }))
        self.lb = {
            "enabled": True, "proxied": True, "steering_policy": "random",
            "session_affinity": "none", "default_pools": ["pool-a", "pool-b"],
            "random_steering": {"default_weight": 0, "pool_weights": {"pool-a": 1, "pool-b": 0}},
        }
        self.mode = "ok"
        self.patches = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        real_client = httpx.AsyncClient
        self.enterContext(patch("server.cloudflare.httpx.AsyncClient", side_effect=lambda **kwargs:
            real_client(transport=httpx.MockTransport(self.upstream), **kwargs)))
        self.client = real_client(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.lifespan = main.lifespan(main.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.close_lifespan)

    async def close_lifespan(self):
        await self.lifespan.__aexit__(None, None, None)

    async def restart(self):
        await self.close_lifespan()
        self.lifespan = main.lifespan(main.app)
        await self.lifespan.__aenter__()

    async def upstream(self, request):
        self.assertEqual(request.headers["Authorization"], "Bearer test-token")
        if request.url.path == "/client/v4/zones/test-zone/load_balancers/test-lb":
            if request.method == "GET":
                if self.mode == "get_timeout":
                    raise httpx.ReadTimeout("test timeout", request=request)
                if self.mode == "blocked":
                    self.entered.set()
                    await self.release.wait()
                if self.mode == "unauthorized":
                    return httpx.Response(403, json={"success": False})
                return httpx.Response(200, json={"success": True, "result": deepcopy(self.lb)})
            if request.method == "PATCH":
                body = json.loads(request.content)
                self.patches.append(body)
                if self.mode != "readback_mismatch":
                    self.lb.update(body)
                if self.mode == "patch_timeout":
                    # Server applied the mutation, but the response was lost.
                    raise httpx.ReadTimeout("test lost response", request=request)
                return httpx.Response(200, json={"success": True, "result": deepcopy(self.lb)})
        if request.method == "GET" and request.url.path in (
            "/client/v4/accounts/test-account/load_balancers/pools/pool-a/health",
            "/client/v4/accounts/test-account/load_balancers/pools/pool-b/health",
        ):
            return httpx.Response(200, json={"success": True, "result": {"pool_id": request.url.path.split("/")[-2], "pop_health": {}}})
        self.fail(f"Unexpected Cloudflare request: {request.method} {request.url}")

    @property
    def auth(self):
        return {"Authorization": "Bearer demo-secret"}

    async def post(self, action, **fields):
        # Use the persisted revision so tests can also post while the upstream GET is paused.
        return await self.client.post("/v1/traffic/actions",
                                      headers={**self.auth, "Idempotency-Key": str(uuid4())},
                                      json={"expectedRevision": len(main.history()), "action": action, **fields})

    async def submit(self, weights=None, kind="SET_WEIGHTS"):
        response = await self.post(kind, **({"weights": weights} if weights is not None else {}))
        self.assertEqual(response.status_code, 202, response.text)
        operation = await self.client.get(response.headers["Location"], headers=self.auth)
        self.assertEqual(operation.status_code, 200)
        return operation.json()

    async def test_health_docs_and_missing_configuration(self):
        with patch.dict(os.environ, {"CF_API_TOKEN": ""}):
            self.assertEqual((await self.client.get("/health")).json(), {"ok": True})
            self.assertEqual((await self.client.get("/docs")).status_code, 200)
            self.assertEqual((await self.client.get("/v1/traffic/status", headers=self.auth)).status_code, 503)
            self.assertEqual((await self.post("SET_WEIGHTS", weights={"aws": 100, "gcp": 0})).status_code, 503)
            self.assertEqual((await self.client.get("/v1/operations/missing", headers=self.auth)).status_code, 404)

    async def test_weights_status_rollback_and_persistence(self):
        for weights in ({"aws": 95, "gcp": 5}, {"aws": 50, "gcp": 50}, {"aws": 0, "gcp": 100}):
            op = await self.submit(weights)
            self.assertEqual(op["status"], "SUCCEEDED")
            self.assertEqual(op["afterWeights"], weights)
            self.assertFalse(op["result"]["traffic_verified"])
            self.assertEqual(self.patches[-1], {"random_steering": {"default_weight": 0, "pool_weights":
                             {"pool-a": weights["aws"] / 100, "pool-b": weights["gcp"] / 100}}})
        rollback = await self.submit(kind="ROLLBACK")
        self.assertEqual(rollback["afterWeights"], {"aws": 50, "gcp": 50})
        await self.restart()
        saved = await self.client.get("/v1/operations/" + op["operationId"], headers=self.auth)
        self.assertEqual(saved.json(), op)
        status = (await self.client.get("/v1/traffic/status", headers=self.auth)).json()
        self.assertEqual(status["weights"], rollback["afterWeights"])
        self.assertEqual(set(status["pool_health"]), {"aws", "gcp"})
        self.assertEqual(status["last_changed_at"], main.history()[0]["finished_at"])

    async def test_invalid_input_does_not_call_cloudflare(self):
        for action, fields, expected in [
            ("SET_WEIGHTS", {}, 400), ("SET_WEIGHTS", {"weights": {"aws": 80, "gcp": 10}}, 400),
            ("SET_WEIGHTS", {"weights": {"aws": -5, "gcp": 105}}, 400),
            ("SET_WEIGHTS", {"weights": {"aws": 100}}, 422),
            ("SET_WEIGHTS", {"weights": {"aws": 95, "gcp": 5, "extra": 0}}, 422),
            ("SET_WEIGHTS", {"weights": {"aws": "NaN", "gcp": 5}}, 400),
            ("ROLLBACK", {"weights": {"aws": 100, "gcp": 0}}, 400),
            ("DRAIN", {"env": "aws"}, 400), ("SET_WEIGHTS", {"weights": {"aws": 100}, "extra": True}, 400),
            ("ROLLBACK", {"transitionId": "tx"}, 400), ("RECONCILE", {"unknownOperationId": "id"}, 400),
            ("ROLLBACK", {}, 409),
        ]:
            with self.subTest(action=action, fields=fields):
                response = await self.post(action, **fields)
                self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self.patches, [])
        self.assertEqual(main.history(), [])

    async def test_concurrent_change_is_rejected(self):
        self.mode = "blocked"
        first = asyncio.create_task(self.submit({"aws": 95, "gcp": 5}))
        try:
            await asyncio.wait_for(self.entered.wait(), timeout=5)
            op_id = main.history()[0]["operation_id"]
            self.assertEqual((await self.client.get("/v1/operations/" + op_id, headers=self.auth)).json()["status"], "RUNNING")
            second = await self.post("SET_WEIGHTS", weights={"aws": 0, "gcp": 100})
            self.assertEqual(second.json()["code"], "RESOURCE_BUSY")
        finally:
            self.release.set()
            result = await asyncio.wait_for(first, timeout=5)
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(len(self.patches), 1)

    async def test_get_timeout_and_forbidden_fail_before_mutation(self):
        for mode in ("get_timeout", "unauthorized"):
            with self.subTest(mode=mode):
                self.mode = mode
                operation = await self.submit({"aws": 95, "gcp": 5})
                self.assertEqual(operation["status"], "FAILED")
                self.assertTrue(operation["error"])
                self.assertEqual((await self.client.get("/v1/traffic/status", headers=self.auth)).status_code, 502)
        self.assertEqual(self.patches, [])

    async def test_readback_mismatch_is_unknown(self):
        self.mode = "readback_mismatch"
        operation = await self.submit({"aws": 0, "gcp": 100})
        self.assertEqual(operation["status"], "UNKNOWN")
        self.assertEqual(operation["afterWeights"], {"aws": 100, "gcp": 0})

    async def test_restart_marks_interrupted_operation_unknown(self):
        await self.submit({"aws": 95, "gcp": 5})
        operation = main.history()[0]
        for state in ("PENDING", "RUNNING"):
            with self.subTest(state=state):
                operation.update(status=state, finished_at=None, result=None)
                main.save(operation)
                await self.restart()
                restored = await self.client.get("/v1/operations/" + operation["operation_id"], headers=self.auth)
                self.assertEqual(restored.json()["status"], "UNKNOWN")
                self.assertIsNotNone(main.history()[0]["finished_at"])

    async def test_v1_reads_and_rolls_back_an_existing_legacy_record(self):
        previous = {"operation_id": "old-op", "type": "set_weights", "status": "SUCCEEDED",
                    "before_weights": {"aws": 100, "gcp": 0}, "after_weights": {"aws": 95, "gcp": 5},
                    "created_at": main.now(), "finished_at": main.now(),
                    "result": {"weights": {"aws": 95, "gcp": 5}, "traffic_verified": False}, "error": None}
        main.save(previous)
        self.lb["random_steering"]["pool_weights"] = {"pool-a": 0.95, "pool-b": 0.05}
        await self.restart()
        old = await self.client.get("/v1/operations/old-op", headers=self.auth)
        self.assertEqual(old.json()["afterWeights"], previous["after_weights"])
        self.assertIsNone(old.json()["migrationId"])
        restored = await self.submit(kind="ROLLBACK")
        self.assertEqual(restored["beforeWeights"], previous["after_weights"])
        self.assertEqual(restored["afterWeights"], previous["before_weights"])

    async def test_openapi_matches_published_contract(self):
        spec = (await self.client.get("/openapi.json")).json()
        path = Path(__file__).resolve().parents[1] / "api" / "openapi.json"
        self.assertEqual(spec, json.loads(path.read_text(encoding="utf-8")))
        command = spec["paths"]["/v1/traffic/actions"]["post"]
        self.assertEqual(command["security"], [{"serviceToken": []}])
        self.assertEqual(set(command["responses"]), {"202", "400", "401", "409", "422", "503"})
        self.assertEqual((await self.client.post("/operations", json={})).status_code, 404)
        self.assertEqual((await self.client.get("/status")).status_code, 404)

    async def test_unsupported_lb_configuration_rejects_mutation(self):
        original = deepcopy(self.lb)
        cases = [
            {"steering_policy": "off"}, {"proxied": False},
            {"session_affinity": "cookie"}, {"default_pools": ["pool-a"]},
            {"enabled": False},
            {"pool_sets": [{"match": {"default": True}, "overrides": {"pools": ["pool-a"]}}]},
            {"rules": [{"disabled": False, "condition": "true"}]},
        ]
        for override in cases:
            with self.subTest(override=override):
                self.lb = {**deepcopy(original), **override}
                operation = await self.submit({"aws": 0, "gcp": 100})
                self.assertEqual(operation["status"], "FAILED")
        self.assertEqual(self.patches, [])

    async def test_platform_auth_replay_and_revision(self):
        with patch.dict(os.environ, {"TRAFFIC_SERVICE_TOKEN": "demo-secret"}):
            route = "/v1/traffic/actions"
            headers = {"Authorization": "Bearer demo-secret", "Idempotency-Key": "k1"}
            body = {"action": "SET_WEIGHTS", "expectedRevision": 0,
                    "migrationId": "app", "transitionId": "tx", "weights": {"aws": 95, "gcp": 5}}
            for path in ("/v1/traffic/status", "/v1/operations/missing"):
                self.assertEqual((await self.client.get(path)).status_code, 401)
            self.assertEqual((await self.client.post(route, json=body)).status_code, 401)
            first = await self.client.post(route, headers=headers, json=body)
            self.assertEqual(first.status_code, 202, first.text)
            self.assertEqual(first.headers["Location"], first.json()["statusUrl"])
            await self.restart()
            again = await self.client.post(route, headers=headers, json=body)
            self.assertEqual(again.json(), first.json())
            changed = await self.client.post(route, headers=headers, json={**body, "weights": {"aws": 50, "gcp": 50}})
            self.assertEqual(changed.json()["code"], "IDEMPOTENCY_CONFLICT")
            headers["Idempotency-Key"] = "new-key"
            stale = await self.client.post(route, headers=headers, json=body)
            self.assertEqual(stale.json()["code"], "REVISION_CONFLICT")
            self.assertEqual(len(self.patches), 1)
            state = await self.client.get("/v1/traffic/status", headers=headers)
            self.assertEqual(state.json()["revision"], 1)
            op = await self.client.get(first.headers["Location"], headers=headers)
            self.assertEqual(op.json()["migrationId"], "app")
            self.assertEqual(op.json()["status"], "SUCCEEDED")
            wrong = await self.client.post(route, headers=headers, json={"action": "ROLLBACK", "expectedRevision": 1})
            self.assertEqual(wrong.json()["code"], "ROLLBACK_SCOPE_CONFLICT")
            undo = await self.client.post(route, headers=headers, json={"action": "ROLLBACK", "expectedRevision": 1,
                                                                       "migrationId": "app", "transitionId": "tx"})
            self.assertEqual(undo.status_code, 202)
            self.assertEqual(len(self.patches), 2)

    async def test_unknown_blocks_until_read_only_reconciliation(self):
        with patch.dict(os.environ, {"TRAFFIC_SERVICE_TOKEN": "demo-secret"}):
            headers = {"Authorization": "Bearer demo-secret", "Idempotency-Key": "set"}
            route = "/v1/traffic/actions"
            body = {"action": "SET_WEIGHTS", "expectedRevision": 0, "weights": {"aws": 0, "gcp": 100}}
            self.mode = "patch_timeout"
            accepted = await self.client.post(route, headers=headers, json=body)
            op_id = accepted.json()["operationId"]
            self.mode = "ok"
            body["expectedRevision"] = 1
            headers["Idempotency-Key"] = "blocked"
            self.assertEqual((await self.client.post(route, headers=headers, json=body)).json()["code"], "UNKNOWN_STATE")
            headers["Idempotency-Key"] = "recovery"
            cmd = {"action": "RECONCILE", "expectedRevision": 1, "unknownOperationId": op_id,
                   "executorStoppedRef": "test:previous-writer-finished"}
            recovery = await self.client.post(route, headers=headers, json=cmd)
            result = await self.client.get(recovery.headers["Location"], headers=headers)
            self.assertEqual(result.json()["result"]["disposition"], "APPLIED")
            self.assertEqual(len(self.patches), 1)
            self.assertEqual((await self.client.get("/v1/traffic/status", headers=headers)).json()["unknownOperationIds"], [])
            headers["Idempotency-Key"] = "rollback"
            undo = await self.client.post(route, headers=headers, json={"action": "ROLLBACK", "expectedRevision": 2})
            op = await self.client.get(undo.headers["Location"], headers=headers)
            self.assertEqual(op.json()["afterWeights"], {"aws": 100, "gcp": 0})



if __name__ == "__main__":
    unittest.main()
