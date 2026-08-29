import json
import logging
import os
import sys
import time

import httpx
from openai import OpenAI
from fastapi import FastAPI, HTTPException

from revenue import calculate_revenue_impact

SERVICE_NAME = "rca-engine"
INCIDENT_ENGINE_URL = "http://incident-engine:8000"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

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

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo only — restrict in real production
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
) if GROQ_API_KEY else None

@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME, "claude_configured": client is not None}


RCA_SYSTEM_PROMPT = """You are an incident investigator for a distributed payments system.

You will be given a structured incident: error patterns, affected services,
timing, and a pre-calculated revenue impact figure.

Rules:
- You must NOT invent, estimate, or recalculate any monetary figures. Use
  the transaction_value_at_risk number exactly as given.
- Your job is to explain the technical root cause using the evidence
  provided, then connect it to the business impact figure.
- Only use the error patterns and services given as evidence. Do not
  invent services, error types, or root causes not present in the data.
- Respond ONLY with valid JSON matching this exact schema, no other text:

{
  "root_cause": "one or two sentence technical explanation",
  "confidence": 0.0,
  "evidence": ["short evidence bullet", "short evidence bullet"],
  "severity": "P1|P2|P3",
  "impact": ["affected service or area", "..."],
  "business_impact_summary": "one sentence connecting technical failure to transaction_value_at_risk",
  "recommended_fix": "concise, bounded, explainable recommendation",
  "alternative_hypotheses": ["other possible explanation", "..."]
}
"""


def build_incident_context(incident: dict, revenue: dict) -> str:
    """
    Compact structured context — NOT raw logs. This is what keeps token
    cost low and RCA quality high/auditable.
    """
    context = {
        "incident_id": incident["incident_id"],
        "incident_window": {
            "start": incident["incident_start"],
            "end": incident["incident_end"],
        },
        "affected_services": incident["affected_services"],
        "total_error_count": incident["total_error_count"],
        "unique_error_patterns": incident["unique_error_patterns"],
        "likely_root_cluster": incident["likely_root_cluster"],
        "error_patterns": incident["error_patterns"],
        "revenue_impact": revenue,
    }
    return json.dumps(context, indent=2)


@app.post("/investigate")
def investigate(minutes: int = 10):
    """
    Full pipeline: fetch correlated incident -> calculate revenue impact
    -> send compact context to Claude -> return structured RCA.
    """
    if client is None:
        raise HTTPException(status_code=500, detail="GROQ_API_KEY not configured")

    start = time.time()

    # 1. Get the correlated incident from incident-engine
    try:
        resp = httpx.post(f"{INCIDENT_ENGINE_URL}/analyze", params={"minutes": minutes}, timeout=15.0)
        resp.raise_for_status()
    except Exception as e:
        log("error", "incident_engine_call_failed", error=str(e))
        raise HTTPException(status_code=502, detail="incident_engine_unavailable")

    incident_data = resp.json()
    incident = incident_data.get("incident")

    if incident is None:
        log("info", "no_incident_to_investigate")
        return {"message": "No incident found in this window — nothing to investigate."}

    # 2. Calculate revenue impact (deterministic, NOT sent to Claude to compute)
    revenue = calculate_revenue_impact(
        incident_start=incident["incident_start"],
        incident_end=incident["incident_end"],
    )
    log("info", "revenue_calculated", incident_id=incident["incident_id"],
        value_at_risk=revenue["transaction_value_at_risk"])

    # 3. Build compact context and call the LLM
    context_str = build_incident_context(incident, revenue)

    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1000,
            messages=[
                {"role": "system", "content": RCA_SYSTEM_PROMPT},
                {"role": "user", "content": context_str},
            ],
        )
        raw_text = completion.choices[0].message.content.strip()
        # Guard against accidental markdown code fences
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        rca_result = json.loads(raw_text)
    except Exception as e:
        log("error", "llm_call_failed", error=str(e))
        raise HTTPException(status_code=502, detail=f"llm_investigation_failed: {e}")

    latency_ms = round((time.time() - start) * 1000, 2)
    log("info", "rca_complete", incident_id=incident["incident_id"],
        confidence=rca_result.get("confidence"), latency_ms=latency_ms)

    return {
        "incident_id": incident["incident_id"],
        "incident_window": {
            "start": incident["incident_start"],
            "end": incident["incident_end"],
        },
        "affected_services": incident["affected_services"],
        "revenue_impact": revenue,
        "rca": rca_result,
    }