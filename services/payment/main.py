import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager

import psycopg2
from psycopg2 import pool
from fastapi import FastAPI, HTTPException

SERVICE_NAME = "payment"

class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)
        return json.dumps(payload)

logger = logging.getLogger(SERVICE_NAME)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)

def log(level, message, **fields):
    getattr(logger, level)(message, extra={"extra_fields": fields})

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "incidentdb")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

POOL_MIN = 1
POOL_MAX = int(os.getenv("PAYMENT_DB_POOL_MAX", "3"))

db_pool = None
FAILURE_MODE = {"db_pool_exhaustion": False}

def init_pool():
    global db_pool
    for attempt in range(10):
        try:
            db_pool = psycopg2.pool.SimpleConnectionPool(
                POOL_MIN, POOL_MAX,
                host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                user=DB_USER, password=DB_PASSWORD,
                connect_timeout=3,
            )
            log("info", "db_pool_initialized", pool_max=POOL_MAX)
            return
        except Exception as e:
            log("warning", "db_pool_init_retry", attempt=attempt, error=str(e))
            time.sleep(2)
    raise RuntimeError("Could not initialize DB pool after retries")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    yield
    if db_pool:
        db_pool.closeall()

app = FastAPI(lifespan=lifespan)

from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)

HELD_CONNECTIONS = []

@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}

@app.post("/admin/inject/db_pool_exhaustion")
def inject_db_pool_exhaustion():
    FAILURE_MODE["db_pool_exhaustion"] = True
    held = 0
    try:
        for _ in range(POOL_MAX):
            conn = db_pool.getconn()
            HELD_CONNECTIONS.append(conn)
            held += 1
    except Exception as e:
        log("error", "injection_partial", error=str(e), held=held)
    log("warning", "failure_injected", failure_type="db_pool_exhaustion", connections_held=held)
    return {"injected": "db_pool_exhaustion", "connections_held": held}

@app.post("/admin/reset")
def reset_failure():
    FAILURE_MODE["db_pool_exhaustion"] = False
    while HELD_CONNECTIONS:
        conn = HELD_CONNECTIONS.pop()
        try:
            db_pool.putconn(conn)
        except Exception:
            pass
    log("info", "failure_reset")
    return {"status": "reset"}

@app.post("/charge")
def charge(order_id: str = None, amount: float = 0.0):
    request_id = str(uuid.uuid4())
    start = time.time()
    conn = None
    try:
        conn = db_pool.getconn(key=None)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
        latency_ms = round((time.time() - start) * 1000, 2)
        log("info", "payment_charged", request_id=request_id, order_id=order_id,
            amount=amount, latency_ms=latency_ms)
        return {"status": "charged", "order_id": order_id, "request_id": request_id}
    except pool.PoolError as e:
        latency_ms = round((time.time() - start) * 1000, 2)
        log("error", "db_pool_exhausted", request_id=request_id, order_id=order_id,
            error=str(e), latency_ms=latency_ms, error_type="DBPoolExhaustion")
        raise HTTPException(status_code=503, detail="db_pool_exhausted")
    except Exception as e:
        latency_ms = round((time.time() - start) * 1000, 2)
        log("error", "payment_failed", request_id=request_id, order_id=order_id,
            error=str(e), latency_ms=latency_ms, error_type=type(e).__name__)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            try:
                db_pool.putconn(conn)
            except Exception:
                pass