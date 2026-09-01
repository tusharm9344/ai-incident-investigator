"""
Jira ticket creation.

Called only after human approval (per project spec — no auto-ticket-per-error
spam). Builds a ticket description from the already-computed RCA + revenue
data; does not call Claude/Groq again.
"""

import os
import httpx

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "")


def jira_configured() -> bool:
    return all([JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY])


def _severity_to_priority(severity: str) -> str:
    return {"P1": "Highest", "P2": "High", "P3": "Medium"}.get(severity, "Medium")


def build_description(incident_id: str, rca: dict, revenue: dict, affected_services: list) -> dict:
    """
    Jira Cloud uses Atlassian Document Format (ADF) for rich text fields.
    Building a simple structured ADF doc here rather than plain text so the
    ticket renders cleanly in the Jira UI.
    """
    def para(text):
        return {"type": "paragraph", "content": [{"type": "text", "text": text}]}

    def bullet_list(items):
        return {
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [para(item)]} for item in items
            ],
        }

    content = [
        para(f"Incident ID: {incident_id}"),
        para(f"Affected services: {', '.join(affected_services)}"),
        para(f"Root cause: {rca['root_cause']}"),
        para(f"Confidence: {round(rca.get('confidence', 0) * 100)}%"),
        para("Evidence:"),
        bullet_list(rca.get("evidence", [])),
        para(
            f"Business impact: {rca.get('business_impact_summary', '')} "
            f"(Transaction Value at Risk: ₹{revenue['transaction_value_at_risk']:,.2f}, "
            f"{revenue['failed_transaction_count']} failed transactions)"
        ),
        para(f"Recommended fix: {rca['recommended_fix']}"),
    ]

    if rca.get("alternative_hypotheses"):
        content.append(para("Alternative hypotheses:"))
        content.append(bullet_list(rca["alternative_hypotheses"]))

    return {"type": "doc", "version": 1, "content": content}


def create_jira_ticket(incident_id: str, rca: dict, revenue: dict, affected_services: list) -> dict:
    """
    Creates a Jira issue via the Cloud REST API v3. Returns the created
    issue's key and a direct browse URL.
    """
    if not jira_configured():
        raise RuntimeError("Jira is not configured (missing env vars)")

    url = f"{JIRA_BASE_URL}/rest/api/3/issue"

    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": f"[{rca['severity']}] {rca['root_cause'][:100]}",
            "description": build_description(incident_id, rca, revenue, affected_services),
            "issuetype": {"name": "Bug"},
            "priority": {"name": _severity_to_priority(rca["severity"])},
        }
    }

    resp = httpx.post(
        url,
        json=payload,
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={"Content-Type": "application/json"},
        timeout=15.0,
    )

    if resp.status_code >= 300:
        raise RuntimeError(f"Jira API error {resp.status_code}: {resp.text}")

    data = resp.json()
    issue_key = data["key"]
    return {
        "issue_key": issue_key,
        "url": f"{JIRA_BASE_URL}/browse/{issue_key}",
    }