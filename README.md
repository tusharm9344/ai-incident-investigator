# AI Incident Investigator

A system that watches a small distributed application, breaks it on purpose
in a repeatable way, figures out **why** it broke, **how much money that
cost**, and asks Claude/an LLM to explain it in plain English — then creates
a Jira ticket after a human says "yes, file it."

---

## 1. The Big Picture

This is the whole system in one diagram. Every box is a separate running
container. Arrows show who talks to whom.

```
                                ┌───────────────────────────-──┐
                                │        YOUR BROWSER          │
                                │   dashboard/index.html       │
                                └───────────────┬──────────────┘
                                                │  clicks "Run Investigation"
                                                ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │                         THE APPLICATION                              │
 │                                                                      │
 │   ┌───────────┐        ┌───────────┐        ┌────────────┐           │
 │   │ Checkout  │──────▶ │ Payment   │──────▶ │ PostgreSQL │           │
 │   │  :8000    │        │  :8001    │        │   :5432    │           │
 │   └─────┬─────┘        └─────┬─────┘        └────────────┘           │
 │         │                    │                                       │
 │         ▼                    │  (writes every attempt,               │
 │   ┌───────────┐              │   success or fail)                    │
 │   │ Inventory │              │                                       │
 │   │  :8002    │              │                                       │
 │   └───────────┘              |.                                      │
 └──────────────────────────────|───────────────────────-------─────────┘
              │  logs + metrics │__
              ▼                    │
 ┌──────────────────────────────-┐ │
 │      OBSERVABILITY            │ │
 │                               │ │
 │  ┌────────┐   ┌────────────┐  │ │
 │  │  Loki  │   │ Prometheus │  │ │
 │  │ :3100  │   │   :9090    │  │ │
 │  └───┬────┘   └─────┬──────┘  │ │
 │      └──────┬────────┘        │ │
 │             ▼                 │ │
 │        ┌─────────┐            │ │
 │        │ Grafana │            │ │
 │        │  :3000  │            │ │
 │        └─────────┘            │ │
 └──────────────┬────────────────┘ │
 (Loki is.      |.                 |
  queried)      |                  │
                ▼                  │
 ┌─────────────────────────────-─┐ │
 │       INCIDENT ENGINE         │ │
 │           :8003               │ │
 │  fingerprints + groups errors │ │
 └──────────────┬────────────────┘ │
                ▼                  │
 ┌─────────────────────────────────┼─-----─┐
 │              RCA ENGINE                 │
 │                :8004                    │
 │                                         │
 │  1. asks Incident Engine for the        │
 │     incident                            │
 │  2. reads PostgreSQL directly.          |
 │     to calculate ₹ at risk.             |
 │  3. sends both to an LLM (Claude/Groq)  |
 │  4. on approval, files a Jira ticket    |
 └───────────────┬─────────────────────────┘
                 ▼
         ┌───────────────┐
         │  Claude/Groq   │
         │  (outside)     │
         └───────────────┘
                 │
                 ▼
         ┌───────────────┐
         │     Jira       │
         │  (outside)     │
         └───────────────┘
```

**In one sentence:** the app breaks → logs/metrics get collected → the
Incident Engine turns raw noise into one clean incident → the RCA Engine
adds a real ₹ cost and asks an LLM to explain it → a human approves → Jira
ticket gets filed.

---

## 2. The Application (where the failure happens)

```
   Browser / curl
        │
        │ POST /order
        ▼
┌───────────────┐        POST /reserve       ┌───────────────┐
│   CHECKOUT    │ ─────────────────────────▶ │   INVENTORY   │
│   port 8000   │                            │   port 8002   │
└───────┬───────┘                            └───────────────┘
        │
        │ POST /charge
        ▼
┌───────────────┐
│    PAYMENT    │
│   port 8001   │  ◀── has only 3 DB connections on purpose
└───────┬───────┘
        │
        ▼
┌───────────────┐
│  POSTGRESQL   │
│   port 5432   │  ◀── stores every payment attempt (success or fail)
└───────────────┘
```

- **Checkout** is the front door. It calls Inventory, then Payment.
- **Payment** is the weak link — it only has **3 database connections**
  available at once. That's intentional.
- **`POST :8001/admin/inject/db_pool_exhaustion`** grabs all 3 connections
  and holds them, so the next real payment can't get one → it fails.
- **`POST :8001/admin/reset`** lets go of them → system heals itself.
- Every single payment attempt — whether it worked or not — gets written to
  a `transactions` table in Postgres. This is what makes the ₹-at-risk
  calculation possible later.

---

## 3. Observability (how we "see" the failure)

```
Every service prints logs                Every service exposes
like a diary entry                        a live health readout
        │                                         │
        ▼                                         ▼
┌───────────────┐                        ┌───────────────┐
│   PROMTAIL    │                        │  PROMETHEUS   │
│ (log shipper) │                        │  port 9090    │
└───────┬───────┘                        └─────-─┬───────┘
        │ ships logs                             │ pulls metrics
        ▼                                        │ every 5 sec
┌───────────────┐                                │
│     LOKI      │◀───────────────────────────────┘
│   port 3100   │        both feed into
└───────┬───────┘
        │
        ▼
┌───────────────┐
│   GRAFANA     │  ◀── one screen to see logs + metrics together
│   port 3000   │
└───────────────┘
```

- **Logs** = "what exactly happened in this one request" (Loki stores these).
- **Metrics** = "how is the system doing overall, over time" (Prometheus
  stores these).
- **Promtail** doesn't do anything smart — it just reads container output
  and forwards it to Loki.
- **Prometheus is pull-based** — it reaches out and *asks* each service
  "what are your numbers?" every 5 seconds, instead of services pushing
  data to it.

---

## 4. Incident Engine (turning noise into one clean story)

```
        1,000+ raw error logs from Loki
                    │
                    ▼
        ┌───────────────────────┐
        │   FINGERPRINTING      │  strips away order_id, timestamps,
        │                       │  amounts — keeps only "what kind
        │                       │  of error is this"
        └───────────┬───────────┘
                    ▼
          ~15-20 unique patterns
                    │
                    ▼
        ┌───────────────────────┐
        │   CORRELATION         │  groups all patterns into ONE
        │                       │  incident, picks the earliest
        │                       │  error as the likely root cause
        └───────────┬───────────┘
                    ▼
          ONE structured incident
          { incident_id, affected_services,
            error_patterns, likely_root_cluster }
```

**Port: 8003.** Endpoint: `POST /analyze?minutes=10` — manually triggered
(not automatic), so a demo is predictable and repeatable.

**Why fingerprinting matters:** 1,000 errors with different order IDs all
*look* different to a computer unless you strip away the noise first. Two
errors with the same service + same error type + same message are almost
certainly the same underlying problem.

---

## 5. RCA Engine (the "brain")

```
┌────────────────────────────────────────────────────────--─┐
│                     RCA ENGINE  (port 8004)               │
│                                                           │
│  Step 1               Step 2                Step 3        │
│  ┌─────────┐          ┌──────────┐          ┌──────────┐  │
│  │  Call   │          | Calculate│          │  Ask the │  │
│  │ Incident│ ──────▶  │  ₹ Value │ ────▶    │  LLM to  │  │
│  │  Engine │          │  at Risk │          │  explain │  │
│  └─────────┘          └──────────┘          └──────────┘  │
│  gets the              reads Postgres         sends a     │
│  structured             directly — the         compact    │
│  incident                LLM never              JSON      │
│                          invents this            summary  │
│                          number                  (not ra  │
│                                                    logs)  │
└─────────────────────────────────────────────────────--────┘
```

**Two endpoints:**
- `POST /investigate?minutes=10` — runs the full pipeline above, returns
  root cause + evidence + confidence + ₹ impact + fix.
- `POST /approve` — only called when a human clicks "Approve" on the
  dashboard. Takes the exact result already shown on screen and files a
  Jira ticket from it. Nothing gets auto-filed.

**Golden rule:** the LLM explains numbers, it never invents them. The ₹
value at risk comes from a real SQL query, not a guess.

---

## 6. Dashboard + Jira (the human-in-the-loop part)

```
┌───────────────┐   POST /investigate   ┌───────────────┐
│   Dashboard   │ ────────────────────▶ │  RCA Engine   │
│ (index.html)  │ ◀──────────────────── │   :8004       │
└───────┬───────┘   shows the result    └───────────────┘
        │
        │  human reads it, clicks "Approve"
        ▼
┌───────────────┐   POST /approve       ┌───────────────┐
│   Dashboard   │ ────────────────────▶ │  RCA Engine   │
│               │                       │   :8004       │
└───────────────┘                       └───────┬───────┘
                                                │
                                                ▼
                                          ┌───────────────┐
                                          │     JIRA      │
                                          │  ticket filed │
                                          └───────────────┘
```

No incident ever becomes a Jira ticket without a person clicking approve
first.

---

## Full Port Reference

    | Service           | Port | What it's for                            |
    |-------------------|------|------------------------------------------|
    | Checkout          | 8000 | Place an order                           |
    | Payment           | 8001 | Charge + inject/reset failure            |
    | Inventory         | 8002 | Reserve stock                            |
    | Incident Engine   | 8003 | Turn raw logs into one incident          |
    | RCA Engine        | 8004 | Investigate + create Jira ticket         |  
    | PostgreSQL        | 5432 | Stores transactions                      |
    | Loki              | 3100 | Stores logs                              |
    | Prometheus        | 9090 | Stores metrics                           |
    | Grafana           | 3000 | View logs + metrics together(admin/admin)|

---

## How to run everything
```bash
docker compose up --build
```
Then open `dashboard/index.html` in a browser.

## How to trigger a failure and watch it get investigated
```bash
curl -X POST "http://localhost:8000/order?order_id=t1&amount=1500"
curl -X POST http://localhost:8001/admin/inject/db_pool_exhaustion
curl -X POST "http://localhost:8000/order?order_id=t2&amount=2000"
curl -X POST "http://localhost:8000/order?order_id=t3&amount=3000"
sleep 5
curl -X POST "http://localhost:8004/investigate?minutes=5"
curl -X POST http://localhost:8001/admin/reset
```
