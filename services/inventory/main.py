import json
import logging
import sys
import time

from fastapi import FastAPI

SERVICE_NAME = "inventory"

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

from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)

@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}

@app.post("/reserve")
def reserve(order_id: str = None):
    start = time.time()
    latency_ms = round((time.time() - start) * 1000, 2)
    log("info", "inventory_reserved", order_id=order_id, latency_ms=latency_ms)
    return {"status": "reserved", "order_id": order_id}