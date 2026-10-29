"""Webhook events: written to the events outbox in the same transaction as the state change,
then delivered asynchronously with signed retries (A5)."""

import hashlib
import hmac
import json
import logging
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ApiClient, ApiClientUser, Event, Task, new_id, utcnow

log = logging.getLogger("agent.webhooks")


def sign(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _iso(dt) -> str | None:
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def task_event(db: Session, event: str, task: Task, data: dict[str, Any] | None = None) -> Event:
    """Queue an event for the API client that owns the task (and log it for audit)."""
    client = db.get(ApiClient, task.api_client_id)
    event_id = new_id("evt")
    payload = {
        "event_id": event_id,
        "event": event,
        "task_id": task.id,
        "external_id": task.external_id,
        "occurred_at": _iso(utcnow()),
        "task_status": task.status,
        "paused": task.paused,
        "followup_count": task.followup_count,
        "next_check_at": _iso(task.next_check_at),
        "data": data or {},
    }
    deliver = bool(client and client.active and client.webhook_url)
    ev = Event(
        event_id=event_id,
        event=event,
        task_id=task.id,
        api_client_id=task.api_client_id,
        payload=payload,
        delivery_status="pending" if deliver else "none",
        next_attempt_at=utcnow() if deliver else None,
    )
    db.add(ev)
    return ev


def user_event(db: Session, event: str, user_id: str, data: dict[str, Any]) -> None:
    """Queue an event for every API client authorized for this user."""
    clients = db.scalars(
        select(ApiClient)
        .join(ApiClientUser, ApiClientUser.api_client_id == ApiClient.id)
        .where(ApiClientUser.user_id == user_id, ApiClient.active.is_(True))
    )
    for client in clients:
        client_event(db, event, client, data, user_id=user_id)


def client_event(
    db: Session,
    event: str,
    client: ApiClient,
    data: dict[str, Any],
    *,
    user_id: str | None = None,
) -> None:
    event_id = new_id("evt")
    payload = {
        "event_id": event_id,
        "event": event,
        "task_id": None,
        "external_id": None,
        "occurred_at": _iso(utcnow()),
        "task_status": None,
        "data": data,
    }
    deliver = bool(client.webhook_url)
    db.add(
        Event(
            event_id=event_id,
            event=event,
            user_id=user_id,
            api_client_id=client.id,
            payload=payload,
            delivery_status="pending" if deliver else "none",
            next_attempt_at=utcnow() if deliver else None,
        )
    )


def audit(db: Session, event: str, task: Task | None, detail: str | None = None, **extra: Any) -> None:
    """Internal timeline entry; never delivered."""
    db.add(
        Event(
            event=event,
            task_id=task.id if task else None,
            payload={"detail": detail, **extra},
            delivery_status="none",
        )
    )


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** (attempts - 1), 3600))


def claim_due(db: Session, limit: int = 50) -> list[tuple[int, str, bytes, str]]:
    """Claim due deliveries (SKIP LOCKED) and push their next attempt out as a lease."""
    now = utcnow()
    rows = db.scalars(
        select(Event)
        .where(Event.delivery_status == "pending", Event.next_attempt_at <= now)
        .order_by(Event.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    out = []
    for ev in rows:
        client = db.get(ApiClient, ev.api_client_id) if ev.api_client_id else None
        if client is None or not client.active or not client.webhook_url:
            ev.delivery_status, ev.last_error = (
                "failed",
                "client inactive or no webhook_url",
            )
            continue
        ev.next_attempt_at = now + timedelta(seconds=120)
        raw = json.dumps(ev.payload, separators=(",", ":"), sort_keys=True).encode()
        out.append((ev.id, client.webhook_url, raw, sign(raw, client.webhook_secret)))
    return out


def deliver_due(session_factory, http: httpx.Client, max_attempts: int) -> int:
    """One delivery pass. No DB transaction is held during HTTP calls."""
    with session_factory() as db:
        batch = claim_due(db)
    delivered = 0
    for ev_id, url, raw, sig in batch:
        error = None
        try:
            resp = http.post(
                url,
                content=raw,
                headers={"Content-Type": "application/json", "X-Signature": sig},
                timeout=10,
            )
            if resp.status_code >= 300:
                error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            error = f"{exc.__class__.__name__}: {exc}"
        with session_factory() as db:
            ev = db.get(Event, ev_id)
            if ev is None:
                continue
            ev.attempts += 1
            if error is None:
                ev.delivery_status, ev.delivered_at, ev.last_error = (
                    "delivered",
                    utcnow(),
                    None,
                )
                delivered += 1
            elif ev.attempts >= max_attempts:
                ev.delivery_status, ev.last_error = "failed", error
                log.warning("Webhook %s failed permanently: %s", ev.event_id, error)
            else:
                ev.last_error, ev.next_attempt_at = (
                    error,
                    utcnow() + backoff(ev.attempts),
                )
    return delivered
