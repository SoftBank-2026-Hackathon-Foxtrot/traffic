"""Run an explicitly requested canary round trip against the Foxtrot test apps."""
import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values


AWS = {"aws": 100, "gcp": 0}
CANARY = {"aws": 95, "gcp": 5}


def change(api, scope, action, revision, weights=None):
    key = f"{scope}-{action}"
    body = {"action": action, "expectedRevision": revision, "migrationId": scope, "transitionId": scope}
    if weights is not None:
        body["weights"] = weights
    print(f"POST /v1/traffic/actions  Idempotency-Key={key}  {json.dumps(body)}", flush=True)
    for attempt in range(2):
        try:
            response = api.post("/v1/traffic/actions", json=body, headers={"Idempotency-Key": key})
        except httpx.TransportError:
            if attempt:
                raise
            continue
        if response.is_server_error and not attempt:
            continue
        response.raise_for_status()
        break

    op_id = response.json()["operationId"]
    print(f"GET /v1/operations/{op_id}", flush=True)
    until = time.monotonic() + 90
    while time.monotonic() < until:
        poll = api.get(f"/v1/operations/{op_id}").raise_for_status()
        op = poll.json()
        if op["status"] == "SUCCEEDED":
            return op
        if op["status"] not in {"QUEUED", "RUNNING"}:
            raise RuntimeError(f"{op_id}: {op['status']} ({op['error']})")
        time.sleep(int(poll.headers.get("Retry-After", "1")))
    raise RuntimeError(f"{op_id}: polling timed out; inspect this operation before continuing")


def status(api):
    return api.get("/v1/traffic/status").raise_for_status().json()


def run(api, probe_url, samples, settle):
    initial = status(api)
    if initial["weights"] != AWS or initial["unknownOperationIds"]:
        raise RuntimeError("This test requires AWS 100%, GCP 0% and no unresolved UNKNOWN operation.")
    for name in AWS:
        pops = initial["pool_health"].get(name, {}).get("pop_health", {})
        if not pops or not all(pop.get("healthy") is True for pop in pops.values()):
            raise RuntimeError(f"No healthy monitor evidence for {name}.")
    scope = "demo-" + uuid4().hex
    op = change(api, scope, "SET_WEIGHTS", initial["revision"], CANARY)
    if op["beforeWeights"] != AWS or op["afterWeights"] != CANARY:
        raise RuntimeError("Unexpected weights. Inspect the operation before changing the route again.")
    time.sleep(settle)
    counts = Counter()
    # A separate client keeps the service credential off the public data path.
    with httpx.Client(timeout=5, limits=httpx.Limits(max_keepalive_connections=0)) as probe:
        for _ in range(samples):
            try:
                r = probe.get(probe_url)
                counts[(r.status_code, r.headers.get("X-Served-By", "unknown"))] += 1
            except httpx.TransportError:
                counts[("error", "transport")] += 1
    print("Observed responses:", dict(counts), flush=True)
    current = status(api)
    if current["revision"] != initial["revision"] + 1 or current["weights"] != CANARY or current["unknownOperationIds"]:
        raise RuntimeError("State changed during sampling. Inspect the LB before rollback.")
    restored = change(api, scope, "ROLLBACK", current["revision"])
    final = status(api)
    if restored["afterWeights"] != AWS or final["weights"] != AWS or final["revision"] != current["revision"] + 1:
        raise RuntimeError("Could not confirm the original weights.")
    print("Confirmed final configured weights:", final["weights"])
    return counts[(200, "aws")] > 0 and counts[(200, "gcp")] > 0 and counts[(200, "aws")] + counts[(200, "gcp")] == samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="run the planned 95:5 change and rollback on the test LB")
    parser.add_argument("--api", default="http://127.0.0.1:8001")
    parser.add_argument("--ping", default="https://app.foxtrot.live/api/ping")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--settle", type=int, default=30, help="wait before sampling (seconds)")
    args = parser.parse_args()
    if args.samples < 1 or args.settle < 0:
        parser.error("samples must be positive and settle nonnegative")
    if not args.apply:
        print("Plan: AWS 100% -> AWS 95% / GCP 5% -> sample X-Served-By -> rollback -> confirm AWS 100%.")
        print("Use --apply only with a dedicated test LB and stateless test apps.")
        return 0
    token = os.getenv("TRAFFIC_SERVICE_TOKEN") or dotenv_values(Path(__file__).resolve().parents[2] / ".env", interpolate=False).get("TRAFFIC_SERVICE_TOKEN")
    if not token:
        parser.error("Set TRAFFIC_SERVICE_TOKEN (environment or ../.env).")
    with httpx.Client(base_url=args.api, headers={"Authorization": f"Bearer {token}"}, timeout=20) as api:
        observed = run(api, args.ping, args.samples, args.settle)
    if not observed:
        print("Sample was incomplete or contained errors; inspect the counts.", file=sys.stderr)
    return 0 if observed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (httpx.HTTPError, RuntimeError, KeyboardInterrupt) as exc:
        sys.exit(f"Stopped: {exc}. Check operation history and current weights before another run.")
