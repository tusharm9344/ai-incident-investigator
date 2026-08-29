import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException

from fingerprint import fingerprint_and_cluster
from correlator import correlate_incident

SERVICE_NAME = "incident-engine"
LOKI_URL = "http://loki:3100"

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


def fetch_logs_from_loki(minutes: int = 10) -> list[dict]:
    """
    Queries Loki for logs from checkout/payment/inventory in the last
    `minutes`, and parses each log line (which is JSON, since our services
    log structured JSON) into a plain dict.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)

    params = {
        "query": '{container=~".*(checkout|payment|inventory).*"}',
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": "5000",
    }

    try:
        resp = httpx.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params, timeout=10.0)
        resp.raise_for_status()
    except Exception as e:
        log("error", "loki_fetch_failed", error=str(e))
        raise HTTPException(status_code=502, detail="loki_unavailable")

    data = resp.json()
    entries = []

    for stream in data.get("data", {}).get("result", []):
        for ts_ns, line in stream.get("values", []):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                # Not a JSON log line (e.g. uvicorn access log) — skip it,
                # the incident engine only cares about our structured logs.
                continue
            # Normalize timestamp to ISO string for easy sorting/display.
            parsed["timestamp"] = datetime.fromtimestamp(
                int(ts_ns) / 1e9, tz=timezone.utc
            ).isoformat()
            entries.append(parsed)

    return entries


@app.post("/analyze")
def analyze(minutes: int = 10):
    """
    Manual trigger: fetch recent logs, fingerprint them into patterns,
    correlate patterns into a single structured incident.

    This is the step-3 output that Revenue-at-Risk (step 4) and the RCA
    engine (step 5) will both consume.
    """
    start = time.time()
    logs = fetch_logs_from_loki(minutes=minutes)
    log("info", "logs_fetched", count=len(logs), window_minutes=minutes)

    clusters = fingerprint_and_cluster(logs)
    log("info", "clusters_built", raw_log_count=len(logs), unique_patterns=len(clusters))

    incident = correlate_incident(clusters)

    latency_ms = round((time.time() - start) * 1000, 2)

    if incident is None:
        log("info", "no_incident_found", latency_ms=latency_ms)
        return {"incident": None, "message": "No error-level patterns found in this window."}

    log("info", "incident_correlated", incident_id=incident["incident_id"],
        error_count=incident["total_error_count"], latency_ms=latency_ms)

    return {"incident": incident}