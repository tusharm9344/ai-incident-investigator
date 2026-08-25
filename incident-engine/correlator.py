"""
Incident correlation.

Goal: take the error clusters (patterns) from fingerprint.py and decide
"do these all belong to the same incident, and if so, what's the likely
upstream cause?"

Approach (deliberately simple, explainable — no ML):
  - Only ERROR-level clusters are considered incident-worthy (INFO logs
    like "payment_charged" are noise for RCA purposes).
  - All error clusters within the fetched time window are treated as ONE
    incident (single-incident-per-analysis is fine for a 12-day MVP demo;
    splitting into multiple concurrent incidents is a stretch goal).
  - The cluster with the EARLIEST first_seen timestamp is treated as the
    most likely upstream/root cause — because in a cascading failure,
    the deepest service fails first and symptoms propagate outward
    afterward (DB pool exhaustion happens before checkout starts timing out).
  - affected_services = every unique service seen across all error clusters.
"""

import uuid


def correlate_incident(clusters: dict) -> dict | None:
    """
    Takes the clusters dict from fingerprint_and_cluster().
    Returns a structured incident dict, or None if no error-level
    clusters were found (i.e. nothing worth investigating).
    """
    error_clusters = [
        c for c in clusters.values()
        if c["error_type"] not in ("INFO", "unknown")
    ]

    if not error_clusters:
        return None

    # Earliest-first-seen cluster = likely root cause (see docstring above).
    error_clusters_sorted = sorted(
        error_clusters,
        key=lambda c: c["first_seen"] or ""
    )
    likely_root_cluster = error_clusters_sorted[0]

    affected_services = sorted({c["service"] for c in error_clusters})

    timestamps = [c["first_seen"] for c in error_clusters if c["first_seen"]] + \
                 [c["last_seen"] for c in error_clusters if c["last_seen"]]
    incident_start = min(timestamps) if timestamps else None
    incident_end = max(timestamps) if timestamps else None

    total_error_count = sum(c["count"] for c in error_clusters)

    incident = {
        "incident_id": str(uuid.uuid4())[:8],
        "incident_start": incident_start,
        "incident_end": incident_end,
        "affected_services": affected_services,
        "total_error_count": total_error_count,
        "unique_error_patterns": len(error_clusters),
        "likely_root_cluster": {
            "service": likely_root_cluster["service"],
            "event": likely_root_cluster["event"],
            "error_type": likely_root_cluster["error_type"],
            "count": likely_root_cluster["count"],
            "first_seen": likely_root_cluster["first_seen"],
        },
        "error_patterns": [
            {
                "service": c["service"],
                "event": c["event"],
                "error_type": c["error_type"],
                "count": c["count"],
                "first_seen": c["first_seen"],
                "last_seen": c["last_seen"],
            }
            for c in error_clusters_sorted
        ],
    }
    return incident