"""
Error fingerprinting.

Goal: turn many raw log lines that represent "the same kind of problem"
into a single fingerprint, so 1000 raw errors become ~15-20 patterns
instead of 1000 unique, unmanageable entries.

Approach (deliberately simple — no ML, no embeddings):
  fingerprint = hash(service + event + error_type)

We do NOT include order_id, request_id, timestamps, or amounts in the
fingerprint — those are exactly the fields that make every error look
"unique" even when the underlying problem is identical. Stripping them
is the normalization step.
"""

import hashlib


def normalize_log(log_entry: dict) -> dict:
    """
    Extract only the fields that define *what kind* of error this is,
    ignoring fields that vary per-request (order_id, request_id, amount,
    timestamp, latency_ms).
    """
    return {
        "service": log_entry.get("service", "unknown"),
        "event": log_entry.get("message", "unknown"),
        "error_type": log_entry.get("error_type", log_entry.get("level", "unknown")),
    }


def compute_fingerprint(log_entry: dict) -> str:
    """
    Deterministic fingerprint: same (service, event, error_type) combo
    always produces the same fingerprint, regardless of order_id/timestamp/etc.
    """
    normalized = normalize_log(log_entry)
    key = f"{normalized['service']}|{normalized['event']}|{normalized['error_type']}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def fingerprint_and_cluster(log_entries: list[dict]) -> dict:
    """
    Takes raw log entries, returns a dict of fingerprint -> cluster info.

    Each cluster tracks: the fingerprint, a representative example log,
    the count, first/last seen timestamps, and the service involved.
    This is the "1000 raw errors -> ~20 patterns" step.
    """
    clusters: dict[str, dict] = {}

    for entry in log_entries:
        fp = compute_fingerprint(entry)
        normalized = normalize_log(entry)
        ts = entry.get("timestamp")

        if fp not in clusters:
            clusters[fp] = {
                "fingerprint": fp,
                "service": normalized["service"],
                "event": normalized["event"],
                "error_type": normalized["error_type"],
                "count": 0,
                "first_seen": ts,
                "last_seen": ts,
                "representative_log": entry,
            }

        cluster = clusters[fp]
        cluster["count"] += 1
        if ts:
            if cluster["first_seen"] is None or ts < cluster["first_seen"]:
                cluster["first_seen"] = ts
            if cluster["last_seen"] is None or ts > cluster["last_seen"]:
                cluster["last_seen"] = ts

    return clusters