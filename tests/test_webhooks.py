"""Signed webhook delivery from the outbox, with retries (A5)."""

import hashlib
import hmac
import json

import httpx
import respx

from app.core import webhooks
from app.db import session_scope
from app.models import Event


def test_events_are_signed_delivered_and_retried(env):
    env.create_task()
    env.c.post(
        f"/v1/tasks/{env.c.get('/v1/tasks', headers=env.h()).json()['data'][0]['id']}/stop",
        headers=env.h(),
    )
    received = []
    with respx.mock() as m:
        route = m.post("https://dash.example.com/webhooks/agent")
        route.side_effect = [httpx.Response(500), httpx.Response(200)]
        http = httpx.Client()
        assert webhooks.deliver_due(session_scope, http, 6) == 0
        with session_scope() as db:
            ev = db.query(Event).filter(Event.delivery_status == "pending").one()
            assert ev.attempts == 1 and ev.last_error == "HTTP 500"
            ev.next_attempt_at = ev.created_at  # make it due now
        assert webhooks.deliver_due(session_scope, http, 6) == 1
        received = [c.request for c in route.calls]
    raw = received[-1].content
    sig = hmac.new(env.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()
    assert received[-1].headers["X-Signature"] == sig
    payload = json.loads(raw)
    for k in [
        "event_id",
        "event",
        "task_id",
        "external_id",
        "occurred_at",
        "task_status",
        "data",
    ]:
        assert k in payload
    assert payload["event"] == "task.stopped"
    assert received[0].content == raw  # retries resend the identical body and event_id


def test_delivery_gives_up_after_max_attempts(env):
    t = env.create_task()
    env.c.post(f"/v1/tasks/{t['id']}/stop", headers=env.h())
    with respx.mock() as m:
        m.post("https://dash.example.com/webhooks/agent").respond(503)
        http = httpx.Client()
        for _ in range(3):
            webhooks.deliver_due(session_scope, http, 3)
            with session_scope() as db:
                for ev in db.query(Event).filter(Event.delivery_status == "pending"):
                    ev.next_attempt_at = ev.created_at
    with session_scope() as db:
        ev = db.query(Event).filter(Event.event == "task.stopped").one()
        assert ev.delivery_status == "failed" and ev.attempts == 3


def test_event_ids_unique_and_state_committed_with_event(env):
    env.seed_thread()
    t = env.create_task()
    env.c.post(f"/v1/tasks/{t['id']}/run", headers=env.h("r"))
    events = env.events()
    assert len({e["event_id"] for e in events}) == len(events)
    assert events[-1]["task_status"] == env.task(t["id"])["status"]


def test_daily_summary(env):
    env.seed_thread()
    t = env.create_task(send_mode="auto")
    env.c.post(f"/v1/tasks/{t['id']}/run", headers=env.h("r"))
    env.engine.daily_summary()
    summary = [e for e in env.events() if e["event"] == "daily.summary"][0]
    assert summary["data"]["sent"] == 1 and summary["data"]["ai_spend_usd"] > 0
