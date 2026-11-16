# Booking Service

Ticket sales for concerts with assigned seating. The core requirement: **a seat is never sold
more than once, under any load**. For popular events the sale opens at a fixed time and tens of
thousands of buyers arrive within the first seconds.

**Stack:** FastAPI, Pydantic v2, SQLAlchemy 2 (async) + asyncpg, Alembic, PostgreSQL 17,
Redis 7, PyJWT + argon2-cffi, structlog, pytest, k6, Docker Compose, uv.

## Quick start

```bash
make up                              # postgres, redis, migrate, api, sweeper
open http://localhost:8080/docs      # Swagger UI

ORG=$(make -s token ROLE=organizer)  # dev JWT signed with the key from the environment
BUY=$(make -s token)
```

```bash
# create an event (organizer)
curl -s localhost:8080/api/v1/events -H "Authorization: Bearer $ORG" -H 'Content-Type: application/json' -d '{
  "name": "Concert", "starts_at": "2027-06-01T19:00:00Z", "sale_starts_at": "2026-01-01T10:00:00Z",
  "categories": [{"code": "VIP", "price": "15000"}, {"code": "STD", "price": "5000"}],
  "seats": [{"row": "1", "number": 1, "category": "VIP"}],
  "rows":  [{"row": "2", "seat_from": 1, "seat_to": 30, "category": "STD"}]
}'
# every seat with its status, plus free seats per category
curl -s localhost:8080/api/v1/events/$EVENT/seats
# hold specific seats, or any N seats in a category
curl -s localhost:8080/api/v1/events/$EVENT/holds -H "Authorization: Bearer $BUY" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: 1' -d '{"seat_ids": ["..."]}'
# confirm (payment is simulated) / cancel
curl -s -X POST   localhost:8080/api/v1/holds/$HOLD/confirm -H "Authorization: Bearer $BUY"
curl -s -X DELETE localhost:8080/api/v1/holds/$HOLD         -H "Authorization: Bearer $BUY"
```

## Development

```bash
uv sync                     # dependencies and dev tools
uv run pre-commit install   # ruff, ruff format, mypy --strict, uv lock on every commit
make test                   # starts postgres + redis from compose and runs pytest
make check                  # lint + typecheck + test
```

Tests run **only against a real Postgres** from compose; SQLite is never used. Row locks,
READ COMMITTED re-checks, `SKIP LOCKED` and advisory locks are exactly what the tests verify.
The schema is created by the Alembic migrations, so the migrations are tested too.

## How double sales are prevented

1. **One source of truth, one clock.** Seat state and every deadline live in Postgres. Business
   rules use only the database's `statement_timestamp()`, never the application servers' clocks.
2. **Locking protocol** (`repositories/booking.py`):
   - hold: advisory lock on `(buyer, event)`, then seat rows `FOR UPDATE` strictly in `seats.id`
     order;
   - confirm and cancel: the hold row `FOR NO KEY UPDATE`, then its seats in the same order;
   - "any N seats" and the sweeper: `FOR UPDATE SKIP LOCKED`, they never wait.

   Every waiting path takes locks in one global order, so deadlocks cannot happen. A waiting
   query re-checks its WHERE clause on the new row version once the lock is released, so a seat
   taken by a competitor simply drops out of the result.
3. **Guards in the schema itself.** `UNIQUE(tickets.seat_id)` (a second ticket for a seat
   cannot be written), `UNIQUE(orders.hold_id)` (one purchase per hold), and a CHECK that keeps a
   seat's status consistent with its owner, hold and order.
4. **Lazy expiry.** A hold past its deadline counts as free everywhere. The sweeper only tidies
   up stored rows; correctness does not depend on it.
5. **Redis is not on the correctness path.** It only backs the seat-map cache and the rate
   limit. Without Redis the service keeps working, just slower (`test_works_without_redis`).

## Business rules

| Rule | Implementation | Test |
|---|---|---|
| One seat, one buyer | locking protocol + `uq_tickets_seat_id` | `test_50_parallel_holds_on_one_seat_exactly_one_succeeds` |
| 10-minute hold | `expires_at = statement_timestamp() + 10 min`, lazy expiry | `test_hold_specific_seats`, `test_expired_hold_frees_seats_without_sweeper` |
| At most 4 seats per buyer, purchases included | advisory lock on (buyer, event), then count | `test_four_seat_limit_is_not_bypassed_by_parallel_requests` |
| Strict deadline | deadline checked after the locks are taken | `test_confirm_after_10_minutes_and_1_second_is_rejected` |
| Idempotent confirm | a repeat gets the same order, `UNIQUE(orders.hold_id)` | `test_five_identical_confirms_give_one_purchase_and_the_same_answer` |
| All or nothing | one transaction, rolled back if any seat is missing | `test_overlapping_seat_sets_at_most_one_succeeds_and_nothing_hangs` |
| No sale before the start | `sale_starts_at <= statement_timestamp()` | `test_sale_not_started` |
| Simulated payment | confirm = successful payment | `test_confirm_sells_the_seats` |

## API

| Method | Path | Who | What |
|---|---|---|---|
| POST | `/api/v1/auth/register`, `/auth/login` | anyone | sign up, get a JWT |
| POST | `/api/v1/events` | organizer | create an event with categories and seats |
| GET | `/api/v1/events/{id}` | anyone | event card and free seats per category |
| GET | `/api/v1/events/{id}/seats` | anyone | every seat's status and the summary (snapshot, <= 1.5 s old) |
| POST | `/api/v1/events/{id}/holds` | buyer | hold seats (`Idempotency-Key`) |
| POST | `/api/v1/holds/{id}/confirm` | buyer | purchase, idempotent |
| DELETE | `/api/v1/holds/{id}` | buyer | cancel, idempotent |
| GET | `/api/v1/me/holds`, `/me/orders`, `/orders/{id}` | buyer | my holds and purchases |
| GET | `/health`, `/health/ready` | - | liveness / readiness |

The buyer is identified by the JWT `sub` claim, the organizer by the `role=organizer` claim
(issued via `python -m app.cli create-user --role organizer` or `issue-token`).

**Errors** have the shape `{"error": {"code", "message", "request_id", "details"}}`.
A business refusal is never a server error:

| HTTP | code |
|---|---|
| 409 | `SEATS_UNAVAILABLE` (taken seats in `details.seat_ids`), `NOT_ENOUGH_SEATS`, `HOLD_LIMIT_EXCEEDED`, `SALE_NOT_STARTED`, `HOLD_RELEASED`, `HOLD_ALREADY_CONFIRMED` |
| 410 | `HOLD_EXPIRED` |
| 401 / 403 | `UNAUTHENTICATED`, `INVALID_CREDENTIALS` / `FORBIDDEN` |
| 404 | `EVENT_NOT_FOUND`, `HOLD_NOT_FOUND`, `ORDER_NOT_FOUND` |
| 422 | `VALIDATION_ERROR`, `INVALID_SEATS`, `INVALID_CATEGORY`, `IDEMPOTENCY_KEY_REUSED` |
| 429 | `RATE_LIMITED` (+ `Retry-After`) |
| 503 | `SERVICE_BUSY` (+ `Retry-After`): overload or lock timeout, safe to retry |

## Code layout

```
src/app/
  api/            HTTP: routes, request/response schemas, DI (deps.py), middleware, error mapping
  services/       business rules (booking, events, auth), transaction boundaries
  repositories/   SQL and locking (SQLAlchemy 2 Core-style queries through AsyncSession)
  db/             ORM models, session, Alembic migrations
  domain/         enums, commands (service input), read models (service output)
  infra/          Redis: two-level snapshot cache (L1 in memory, L2 Redis, gzip), rate limit
  core/           settings, domain errors, logging, JWT and passwords
  container.py    composition root: process-wide dependencies
  workers/        sweeper for expired holds
  invariants.py   SQL audit of invariants after a load run
  cli.py          migrate | sweeper | create-user | issue-token | check-invariants
```

Logs are JSON on stdout. Every line carries `request_id` (taken from `X-Request-ID` or
generated) and, once authenticated, `user_id`. This applies to uvicorn and SQLAlchemy logs too.

## Load test

```bash
make up && make loadtest   # k6: 10 000 buyers, 1000 seats, then a database audit
```

The scenario is `loadtest/onsale.js`. Every buyer arrives within 3 s of the sale opening,
looks at the seat map and holds 1-4 seats: 75% pick specific seats, 25% take "any N". After a
refusal the buyer refreshes the map and tries again. Then 85% pay (30% of them press "confirm"
twice), 10% cancel and 5% walk away. The 10 000 buyers run as async journeys on 200 k6 VUs:
real concurrency without 10 000 JS runtimes. A report with RPS, p50/p95/p99 and the share of
each response type is written to `loadtest/results/report.md`.

**Current results** (everything on one laptop under Docker Desktop, 12 vCPU, k6 and the service
together):

- **Correctness: met.** The audit after every run found no violations: zero
  double sales, at most 4 seats per buyer, no partial or stuck holds, never more seats sold
  than the hall has.
- **Latency (target: hold p95 <= 300 ms): not met yet.** The best run reached p95 = 1.25 s with
  0% 5xx. The bottleneck is the API workers' CPU (530% of 600% at peak) while Postgres sits at
  about 17%, and the load generator shares the machine. Next steps: fewer round trips and less
  ORM overhead on the hold hot path, and a measurement from a separate machine.

## AWS deployment (plan)

ECS Fargate (`api` and `sweeper` services from one image) + RDS PostgreSQL + ElastiCache Redis
+ ALB. Migrations run as a one-off task, `python -m app.cli migrate`, before each rollout.
Details that matter:

- `UVICORN_TIMEOUT_KEEP_ALIVE` must exceed the ALB idle timeout (60 s);
- behind RDS Proxy or pgbouncer in transaction mode, asyncpg's prepared statement cache must be
  turned off;
- JSON logs with `request_id` are directly queryable in CloudWatch Logs Insights.
