"""실험용 트래픽 제어 API. 단일 프로세스(--workers 1)로 실행한다."""
import json
import math
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import contract

from .cloudflare import Cloudflare

DB_PATH = Path(os.getenv("TRAFFIC_DB_PATH", "server/traffic.db"))


def now():
    return datetime.now(timezone.utc).isoformat()


def save(operation):
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute("INSERT OR REPLACE INTO operations VALUES (?, ?)",
                   (operation["operation_id"], json.dumps(operation)))


def history():
    with closing(sqlite3.connect(DB_PATH)) as db:
        rows = db.execute("SELECT data FROM operations").fetchall()
    return sorted((json.loads(row[0]) for row in rows), key=lambda op: op["created_at"], reverse=True)


@asynccontextmanager
async def lifespan(app):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    for operation in history():
        if operation["status"] in ("PENDING", "RUNNING"):
            operation.update(status="FAILED" if operation["type"] == "reconcile" else "UNKNOWN", finished_at=now(), error="서버가 재시작되었습니다. 실제 LB 상태를 확인하세요.")
            save(operation)
    yield


app = FastAPI(title="Foxtrot Traffic Control API", version="1.0", lifespan=lifespan)


def cloudflare():
    try:
        return Cloudflare()
    except ValueError as error:
        raise HTTPException(503, str(error)) from error


async def execute(operation, cf):
    operation.update(status="RUNNING", started_at=now())
    save(operation)
    patch_started = False
    try:
        before = cf.weights(await cf.get_lb())
        operation["before_weights"] = before
        save(operation)
        patch_started = True
        await cf.set_weights(operation["requested_weights"])
        after = cf.weights(await cf.get_lb())
        operation["after_weights"] = after
        if not all(math.isclose(after[name], value, abs_tol=0.000001)
                   for name, value in operation["requested_weights"].items()):
            raise ValueError("설정 재조회 결과가 요청한 가중치와 다릅니다.")
        operation.update(status="SUCCEEDED", result={"weights": after, "traffic_verified": False})
    except Exception as error:
        # PATCH 이후 오류는 실제 적용 여부가 불명확하므로 자동 재시도하지 않는다.
        operation.update(status="UNKNOWN" if patch_started else "FAILED", error=str(error))
    finally:
        operation["finished_at"] = now()
        save(operation)


@app.get("/health")
async def health():
    return {"ok": True}


service_token = HTTPBearer(scheme_name="serviceToken", auto_error=False)


def authorize(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(service_token)]):
    token = os.getenv("TRAFFIC_SERVICE_TOKEN")
    if not token:
        raise HTTPException(503, "TRAFFIC_SERVICE_TOKEN 설정이 필요합니다.")
    if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), token.encode()):
        raise HTTPException(401, "UNAUTHORIZED", headers={"WWW-Authenticate": "Bearer"})


def error_response(description):
    return {"model": contract.Error, "description": description}


v1 = APIRouter(prefix="/v1", dependencies=[Depends(authorize)], responses={
    401: error_response("UNAUTHORIZED"),
    503: error_response("SERVICE_UNAVAILABLE"),
})
retry_after = {"Retry-After": {"description": "Seconds until next poll", "schema": {"type": "integer"}}}


def unresolved(operations):
    resolved = {op.get("resolves") for op in operations if op["status"] == "SUCCEEDED"}
    return [op for op in operations if op["status"] == "UNKNOWN" and op["operation_id"] not in resolved]


def last_applied(operations):
    resolved = {op["resolves"] for op in operations if op.get("resolves") and op["status"] == "SUCCEEDED"
                and op["result"]["disposition"] == "APPLIED"}
    return next((op for op in operations if op["type"] != "reconcile" and
                 (op["status"] == "SUCCEEDED" or op["operation_id"] in resolved)), None)


def previous_weights(operations, scope):
    previous = last_applied(operations)
    if previous is None or previous["before_weights"] is None:
        raise HTTPException(409, "롤백할 성공 이력이 없습니다.")
    if any(scope.get(k) != previous.get("command", {}).get(k) for k in ("migrationId", "transitionId")):
        raise HTTPException(409, "ROLLBACK_SCOPE_CONFLICT")
    return previous["before_weights"]


def platform_error(status_code, code, message, headers=None):
    return JSONResponse({"code": code, "message": message, "operationId": None, "blockers": []},
                        status_code=status_code, headers=headers)


@app.exception_handler(HTTPException)
async def api_error(request: Request, error: HTTPException):
    fallback = {404: "NOT_FOUND", 409: "INVALID_STATE", 422: "PRECONDITION_FAILED",
                502: "UPSTREAM_ERROR", 503: "SERVICE_UNAVAILABLE"}
    detail = str(error.detail)
    code = detail if detail.isascii() and detail.isupper() else fallback.get(error.status_code, "REQUEST_ERROR")
    return platform_error(error.status_code, code, detail, error.headers)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError):
    return platform_error(400, "INVALID_REQUEST", "요청 본문과 필수 헤더를 확인하세요.")


async def reconcile(operation, previous, cf):
    operation.update(status="RUNNING", started_at=now())
    save(operation)
    try:
        actual = cf.weights(await cf.get_lb())
        operation["after_weights"] = actual
        before = previous["before_weights"]
        intended = previous["requested_weights"]

        def matches(expected):
            return expected is not None and actual.keys() == expected.keys() and all(
                math.isclose(value, expected[name], abs_tol=0.000001) for name, value in actual.items()
            )

        # Before PATCH the executor durably saves before_weights. No snapshot means
        # this executor did not send PATCH. Never send an external mutation here.
        if before is None or matches(before):
            disposition = "NO_CHANGE"
        elif matches(intended):
            disposition = "APPLIED"
        else:
            raise ValueError("관측 비율이 변경 전·요청 비율 모두와 다릅니다.")
        operation.update(status="SUCCEEDED", result={"weights": actual, "disposition": disposition,
                                                      "traffic_verified": False})
    except Exception as error:
        operation.update(status="FAILED", error=str(error))
    finally:
        operation["finished_at"] = now()
        save(operation)


def platform_view(op):
    return {"operationId": op["operation_id"], "action": op["type"].upper(),
            "status": "QUEUED" if op["status"] == "PENDING" else op["status"],
            "beforeWeights": op["before_weights"], "afterWeights": op["after_weights"],
            "migrationId": op.get("command", {}).get("migrationId"),
            "transitionId": op.get("command", {}).get("transitionId"),
            "result": op["result"], "error": op["error"],
            "startedAt": op.get("started_at"), "finishedAt": op.get("finished_at")}


@v1.get("/traffic/status", response_model=contract.Status, operation_id="getTrafficStatus",
        responses={502: error_response("UPSTREAM_ERROR")})
async def platform_status():
    ops = history()
    last = last_applied(ops)
    cf = cloudflare()
    try:
        weights = cf.weights(await cf.get_lb())
        health = await cf.health()
    except Exception as error:
        raise HTTPException(502, str(error)) from error
    return {"revision": len(ops), "unknownOperationIds": [op["operation_id"] for op in unresolved(ops)],
            "weights": weights, "pool_health": health, "observedAt": now(), "last_changed_at": last["finished_at"] if last else None}


@v1.post("/traffic/actions", status_code=202, response_model=contract.Accepted, operation_id="submitTrafficAction",
         responses={202: {"headers": {**retry_after, "Location": {"schema": {"type": "string"}}}},
                    400: error_response("INVALID_REQUEST"),
                    409: error_response("REVISION_CONFLICT, IDEMPOTENCY_CONFLICT, RESOURCE_BUSY, UNKNOWN_STATE, "
                                        "INVALID_STATE or ROLLBACK_SCOPE_CONFLICT"),
                    422: error_response("PRECONDITION_FAILED")})
async def platform_action(command: Annotated[contract.SetWeights | contract.Rollback | contract.Reconcile,
                                             Body(discriminator="action")],
                          tasks: BackgroundTasks, response: Response,
                          idempotency_key: Annotated[str, Header(min_length=1, max_length=255)]):
    ops = history()
    body = command.model_dump(exclude_none=True)
    # One authenticated service principal, one LB, one worker. No await between
    # duplicate lookup and durable admission. The key and operation share one row.
    duplicate = next((op for op in ops if op.get("idempotency_key") == idempotency_key), None)
    if duplicate:
        if duplicate["command"] != body:
            raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
        op_id = duplicate["operation_id"]
    else:
        if command.expectedRevision != len(ops):
            raise HTTPException(409, "REVISION_CONFLICT")
        cf = cloudflare()
        if any(op["status"] in ("RUNNING", "PENDING") for op in ops):
            raise HTTPException(409, "RESOURCE_BUSY")
        unknown = unresolved(ops)
        op_id = str(uuid4())
        op = {"operation_id": op_id, "type": command.action.lower(), "status": "PENDING",
              "idempotency_key": idempotency_key, "command": body, "requested_by": "platform",
              "before_weights": None, "requested_weights": None, "after_weights": None,
              "created_at": now(), "started_at": None, "finished_at": None, "error": None, "result": None}
        if isinstance(command, contract.Reconcile):
            previous = next((item for item in unknown if item["operation_id"] == command.unknownOperationId), None)
            if previous is None:
                raise HTTPException(409, "INVALID_STATE")
            op["resolves"] = previous["operation_id"]
            save(op)
            tasks.add_task(reconcile, op, previous, cf)
        else:
            if unknown:
                raise HTTPException(409, "UNKNOWN_STATE")
            weights = previous_weights(ops, body) if isinstance(command, contract.Rollback) else command.weights
            if set(weights) != set(cf.pools):
                raise HTTPException(422, "weights에는 CF_POOL_IDS에 등록한 환경을 모두 지정하세요.")
            op["requested_weights"] = weights
            save(op)
            tasks.add_task(execute, op, cf)

    url = f"/v1/operations/{op_id}"
    response.headers["Location"] = url
    response.headers["Retry-After"] = "1"
    return {"operationId": op_id, "action": command.action, "status": "QUEUED", "statusUrl": url}


@v1.get("/operations/{operation_id}", response_model=contract.Operation, operation_id="getTrafficOperation",
        responses={200: {"headers": retry_after}, 404: error_response("NOT_FOUND"),
                   "4XX": error_response("Invalid request")})
async def platform_operation(operation_id: str, response: Response):
    with closing(sqlite3.connect(DB_PATH)) as db:
        row = db.execute("SELECT data FROM operations WHERE id = ?", (operation_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    op = json.loads(row[0])
    if op["status"] in ("PENDING", "RUNNING"):
        response.headers["Retry-After"] = "1"
    return platform_view(op)


app.include_router(v1)
