"""Stand-in app for the Cloudflare load balancer experiments.

Behaves like the real app (demo/app/main.py) on the parts the traffic layer depends on: an
X-Served-By header on every response, /readyz for the health monitor, and a drain on SIGTERM.
It has no stores, so any failure seen in an experiment belongs to the traffic layer.
"""
import asyncio
import os
import signal
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response

SERVED_BY = os.environ.get("SERVED_BY", "local")
DRAIN_S = float(os.environ.get("DRAIN_S", "6"))    # time to leave the load balancer, same as demo
NOT_READY_FILE = Path(os.environ.get("NOT_READY_FILE", "/tmp/testapp-not-ready"))
MAX_SLOW_S = 30
_stopping = threading.Event()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """SIGTERM: /readyz turns 503 at once, and the server only starts stopping after the drain."""
    previous = signal.getsignal(signal.SIGTERM)

    def on_term(signum, frame):
        _stopping.set()
        threading.Timer(DRAIN_S, previous, (signum, frame)).start()

    if callable(previous):
        signal.signal(signal.SIGTERM, on_term)
    yield


app = FastAPI(title="foxtrot testapp", lifespan=lifespan)


@app.middleware("http")
async def served_by(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Served-By"] = SERVED_BY
    return response


@app.get("/")
def index():
    return {"served_by": SERVED_BY, "host": socket.gethostname()}


@app.get("/readyz")
def readyz(response: Response):
    """503 while stopping, or while NOT_READY_FILE exists (touch it to fail the health monitor)."""
    if _stopping.is_set():
        response.status_code = 503
        return {"ready": False, "reason": "stopping"}
    if NOT_READY_FILE.exists():
        response.status_code = 503
        return {"ready": False, "reason": "manual"}
    return {"ready": True}


@app.get("/api/ping")
def ping():
    return {"pong": True, "served_by": SERVED_BY}


@app.get("/slow")
async def slow(seconds: float = 5):
    """Holds a request open, to see whether in-flight requests survive a drain."""
    seconds = min(max(seconds, 0), MAX_SLOW_S)
    await asyncio.sleep(seconds)
    return {"slept": seconds, "served_by": SERVED_BY}
