import json
import logging
import sys
import time
import uuid

import httpx
from fastapi import FastAPI, HTTPException

SERVICE_NAME = "checkout"
PAYMENT_URL = "http://payment:8000"
INVENTORY_URL = "http://inventory:8000"

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

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}

@app.post("/order")
def place_order(order_id: str = None, amount: float = 0.0):
    order_id = order_id or str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    start = time.time()

    try:
        inv_resp = httpx.post(f"{INVENTORY_URL}/reserve", params={"order_id": order_id}, timeout=3.0)
        inv_resp.raise_for_status()
    except Exception as e:
        log("error", "inventory_check_failed", request_id=request_id, order_id=order_id,
            error=str(e), error_type=type(e).__name__)
        raise HTTPException(status_code=502, detail="inventory_unavailable")

    try:
        pay_resp = httpx.post(f"{PAYMENT_URL}/charge",
                               params={"order_id": order_id, "amount": amount},
                               timeout=2.0)
        pay_resp.raise_for_status()
    except httpx.TimeoutException as e:
        latency_ms = round((time.time() - start) * 1000, 2)
        log("error", "payment_call_timeout", request_id=request_id, order_id=order_id,
            error=str(e), error_type="PaymentTimeout", latency_ms=latency_ms)
        raise HTTPException(status_code=504, detail="payment_timeout")
    except httpx.HTTPStatusError as e:
        latency_ms = round((time.time() - start) * 1000, 2)
        log("error", "payment_call_failed", request_id=request_id, order_id=order_id,
            error=str(e), error_type="PaymentServiceError",
            status_code=e.response.status_code, latency_ms=latency_ms)
        raise HTTPException(status_code=502, detail="payment_failed")

    latency_ms = round((time.time() - start) * 1000, 2)
    log("info", "order_placed", request_id=request_id, order_id=order_id,
        amount=amount, latency_ms=latency_ms)
    return {"status": "order_placed", "order_id": order_id, "request_id": request_id}