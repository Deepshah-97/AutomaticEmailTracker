# Follow-Up Agent

Watches Gmail threads for clients who have gone quiet, drafts a short follow-up with Claude, and sends it from
the owner's own Gmail after approval (or automatically in `auto` mode). It stops as soon as the client replies,
a meeting is booked, the task is closed, or the client opts out.

Implements the Follow-Up Agent Build Spec v1.1. The HTTP contract is [`openapi.yaml`](openapi.yaml); the
demo dashboard keeps a copy of it.

## Stack

Python 3.12, FastAPI, SQLAlchemy 2 + Alembic on PostgreSQL 16, APScheduler, the Anthropic SDK
(`claude-sonnet-5-5` with structured outputs), and the Gmail API.

## Run locally

```bash
cp .env.example .env          # fill in the keys; see comments in the file
docker compose up --build     # Postgres + agent on http://localhost:8000
```

Or without Docker, against a local Postgres:

```bash
uv sync
uv run alembic upgrade head
uv run uvicorn --factory app.main:create_app --reload
```

API docs are served at `/docs`.

### First-time setup

All admin calls use `Authorization: Bearer $ADMIN_API_KEY`.

```bash
# 1. Add each salesperson
curl -X POST localhost:8000/v1/admin/users -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H 'Content-Type: application/json' -d '{"email":"sam@company.com","name":"Sam Sender","timezone":"America/New_York"}'

# 2. Create an API client for the dashboard (the key and webhook secret are shown once)
curl -X POST localhost:8000/v1/admin/api-clients -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Demo dashboard","webhook_url":"https://dashboard.example.com/webhooks/agent","user_emails":["sam@company.com"]}'

# 3. Each user connects Gmail: open the URL returned by
#    GET /v1/users/{user_id}/connect  (API client key)
```

Then turn on `SENDING_ENABLED=true` when you are ready for real emails to go out.

## How it works

- **Checks** run hourly. A task becomes due after its wait (business days, owner's time zone) from the
  scenario baseline. The thread is re-read from Gmail; any client reply stops the task.
- **Drafts** are written by Claude from the thread, notes and the owner's style examples. A safety check rejects
  placeholders, invented numbers, dates or offers, AI mentions, and more than 150 words; anything doubtful is
  flagged `needs_review`.
- **Sending** happens only inside 08:30–17:30 on weekdays in the owner's time zone. Gmail is re-read right
  before every send. A send that may or may not have gone through becomes `delivery_unknown` and is matched by
  its Message-ID; it is never resent automatically.
- **Webhooks** go out from an outbox, signed with `X-Signature` (hex HMAC-SHA256 of the raw body), retried with
  backoff, and deduplicated by `event_id` on the receiver.
- **Safety switches**: `SENDING_ENABLED` (off by default), `POST /v1/admin/pause-all`, per-user and per-task
  pause, and `TEST_MODE` with an address allowlist for demos.

Background jobs (`SCHEDULER_ENABLED=true` on one instance only): checks hourly, sends every minute, webhooks
every 15 s, stuck-send recovery every 10 min, alerts hourly, daily summary, and a nightly retention purge
that blanks email bodies after 180 days.

## Tests

The tests use a real Postgres (they create and drop a `followup_test` database) with fake Gmail and Claude.

```bash
docker compose up -d db
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
