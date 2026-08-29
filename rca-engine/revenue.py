"""
Revenue-at-Risk calculation.

CRITICAL RULE (per project spec): this number must be deterministic and
auditable. Claude receives the calculated value as evidence and explains
it — Claude never computes or estimates it itself.

"Transaction Value at Risk" is used instead of "lost revenue" because a
failed payment may later succeed through retry/recovery.
"""

import os
import psycopg2

DB_HOST = os.getenv("DB_HOST", "postgres")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "incidentdb")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")


def _get_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD, connect_timeout=5,
    )


def calculate_revenue_impact(incident_start: str, incident_end: str) -> dict:
    """
    Queries the transactions table for rows created within the incident's
    time window and computes attempted/successful/failed/at-risk totals.

    Returns a plain dict of numbers — this is what gets embedded into the
    structured incident and handed to Claude as evidence.
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT status, COALESCE(SUM(amount), 0), COUNT(*)
            FROM transactions
            WHERE created_at BETWEEN %s AND %s
            GROUP BY status
            """,
            (incident_start, incident_end),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    success_value = 0.0
    success_count = 0
    failed_value = 0.0
    failed_count = 0

    for status, total_amount, count in rows:
        if status == "success":
            success_value = float(total_amount)
            success_count = count
        elif status == "failed":
            failed_value = float(total_amount)
            failed_count = count

    total_attempted_value = success_value + failed_value
    total_attempted_count = success_count + failed_count

    # As specified: transaction_value_at_risk = failed transaction value
    # (recovery tracking is a future extension, not built here).
    transaction_value_at_risk = failed_value

    return {
        "incident_window": {"start": incident_start, "end": incident_end},
        "total_attempted_value": round(total_attempted_value, 2),
        "total_attempted_count": total_attempted_count,
        "successful_transaction_value": round(success_value, 2),
        "successful_transaction_count": success_count,
        "failed_transaction_value": round(failed_value, 2),
        "failed_transaction_count": failed_count,
        "transaction_value_at_risk": round(transaction_value_at_risk, 2),
        "currency": "INR",
    }