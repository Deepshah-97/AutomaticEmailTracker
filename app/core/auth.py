"""API-key authentication, per-client scoping, admin auth and rate limiting."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import session_scope
from ..models import ApiClient, ApiClientUser, Task, User
from .errors import ApiError


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    return "fua_" + secrets.token_urlsafe(32)


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, per_minute: int) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= per_minute:
            raise ApiError(429, "rate_limited", "Too many requests", headers={"Retry-After": "60"})
        hits.append(now)


rate_limiter = RateLimiter()


@dataclass
class Caller:
    client: ApiClient
    user_ids: frozenset[str]

    def can_act_for(self, user_id: str) -> bool:
        return user_id in self.user_ids


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise ApiError(401, "unauthorized", "Missing bearer token")
    return header[7:].strip()


def require_client(request: Request) -> Caller:
    settings: Settings = request.app.state.settings
    token = _bearer(request)
    # Own short session, so routes that call Gmail or Claude don't hold a transaction open.
    with session_scope() as db:
        client = db.scalar(select(ApiClient).where(ApiClient.api_key_hash == hash_key(token)))
        if client is None or not client.active:
            raise ApiError(401, "unauthorized", "Invalid or revoked API key")
        user_ids = frozenset(db.scalars(select(ApiClientUser.user_id).where(ApiClientUser.api_client_id == client.id)))
    rate_limiter.check(client.id, settings.rate_limit_per_minute)
    request.state.api_client_id = client.id
    return Caller(client=client, user_ids=user_ids)


def require_admin(request: Request) -> None:
    settings: Settings = request.app.state.settings
    token = _bearer(request)
    if not settings.admin_api_key or not hmac.compare_digest(token, settings.admin_api_key):
        raise ApiError(401, "unauthorized", "Admin key required")
    rate_limiter.check("admin", settings.rate_limit_per_minute)


def scoped_task(db: Session, caller: Caller, task_id: str, *, lock: bool = False) -> Task:
    """Load a task the caller may see. Out-of-scope tasks look exactly like missing ones."""
    q = select(Task).where(Task.id == task_id, Task.api_client_id == caller.client.id)
    if lock:
        q = q.with_for_update()
    task = db.scalar(q)
    if task is None or not caller.can_act_for(task.owner_user_id):
        raise ApiError(404, "not_found", "Task not found")
    return task


def scoped_user(db: Session, caller: Caller, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None or not user.active:
        raise ApiError(404, "not_found", "User not found")
    if not caller.can_act_for(user.id):
        raise ApiError(403, "forbidden", "This API client is not authorized for that user")
    return user
