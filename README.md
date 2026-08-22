# AI Incident Investigator

Day 1: Checkout, Payment, Inventory services under Docker Compose with Postgres.
Payment has a small connection pool (3) and an injection endpoint to simulate
pool exhaustion on demand.

## Run
docker compose up --build

## Test
POST http://localhost:8000/order?order_id=abc&amount=100
POST http://localhost:8001/admin/inject/db_pool_exhaustion
POST http://localhost:8001/admin/reset