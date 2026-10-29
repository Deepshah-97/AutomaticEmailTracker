"""Admin operations: emergency stop, export, and provisioning users and API clients."""

import secrets

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.auth import generate_api_key, hash_key, require_admin
from ..core.errors import ApiError
from ..db import get_db
from ..models import ApiClient, ApiClientUser, AppSetting, Event, Followup, Task, User
from ..schemas import followup_out, iso, task_out, user_out, valid_timezone

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class PauseAllIn(BaseModel):
    paused: bool = True


class UserIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1)
    timezone: str = "America/New_York"
    signature: str = ""
    send_mode: str = "approve"


class ClientIn(BaseModel):
    name: str = Field(min_length=1)
    webhook_url: str | None = None
    user_emails: list[EmailStr] = []


class ClientUsersIn(BaseModel):
    user_emails: list[EmailStr]


@router.post("/pause-all")
def pause_all(body: PauseAllIn | None = None, db: Session = Depends(get_db)) -> dict:
    paused = True if body is None else body.paused
    row = db.get(AppSetting, "pause_all") or AppSetting(key="pause_all", value="false")
    row.value = "true" if paused else "false"
    db.add(row)
    db.add(Event(event="admin.pause_all", payload={"paused": paused}))
    return {"paused": paused}


@router.get("/export")
def export(db: Session = Depends(get_db)) -> dict:
    """All company data as JSON. Tokens, key hashes and webhook secrets are never exported."""
    users = {u.id: u for u in db.scalars(select(User))}
    return {
        "users": [user_out(u) | {"signature": u.signature, "style_examples": u.style_examples} for u in users.values()],
        "tasks": [
            task_out(t, users[t.owner_user_id].email) | {"notes": t.notes, "goal": t.goal}
            for t in db.scalars(select(Task))
        ],
        "followups": [
            followup_out(f) | {"context_summary": f.context_summary, "feedback": f.feedback}
            for f in db.scalars(select(Followup))
        ],
        "events": [
            {
                "event_id": e.event_id,
                "event": e.event,
                "task_id": e.task_id,
                "created_at": iso(e.created_at),
                "payload": e.payload,
            }
            for e in db.scalars(select(Event).order_by(Event.id))
        ],
    }


@router.post("/users", status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db)) -> dict:
    if db.scalar(select(User).where(User.email == str(body.email).lower())):
        raise ApiError(409, "exists", "User already exists")
    try:
        valid_timezone(body.timezone)
    except ValueError as e:
        raise ApiError(422, "invalid_timezone", str(e)) from e
    user = User(
        email=str(body.email).lower(),
        name=body.name,
        timezone=body.timezone,
        signature=body.signature,
        send_mode=body.send_mode if body.send_mode in ("approve", "auto") else "approve",
    )
    db.add(user)
    db.flush()
    return user_out(user)


@router.post("/api-clients", status_code=201)
def create_client(body: ClientIn, db: Session = Depends(get_db)) -> dict:
    if body.webhook_url and not body.webhook_url.startswith(("https://", "http://dashboard", "http://localhost")):
        raise ApiError(422, "insecure_webhook_url", "webhook_url must use https")
    key = generate_api_key()
    secret = secrets.token_urlsafe(32)
    client = ApiClient(
        name=body.name,
        api_key_hash=hash_key(key),
        webhook_url=body.webhook_url,
        webhook_secret=secret,
    )
    db.add(client)
    db.flush()
    _grant(db, client.id, [str(e) for e in body.user_emails])
    # The key and secret are shown once and never stored in plain text (the secret is needed to sign).
    return {
        "id": client.id,
        "name": client.name,
        "api_key": key,
        "webhook_secret": secret,
        "webhook_url": client.webhook_url,
    }


@router.post("/api-clients/{client_id}/users")
def grant_users(client_id: str, body: ClientUsersIn, db: Session = Depends(get_db)) -> dict:
    if db.get(ApiClient, client_id) is None:
        raise ApiError(404, "not_found", "API client not found")
    _grant(db, client_id, [str(e) for e in body.user_emails])
    return {"ok": True}


@router.post("/api-clients/{client_id}/revoke")
def revoke(client_id: str, db: Session = Depends(get_db)) -> dict:
    client = db.get(ApiClient, client_id)
    if client is None:
        raise ApiError(404, "not_found", "API client not found")
    client.active = False
    return {"id": client.id, "active": False}


def _grant(db: Session, client_id: str, emails: list[str]) -> None:
    for email in emails:
        user = db.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            raise ApiError(404, "user_not_found", f"No user {email}")
        if not db.get(ApiClientUser, (client_id, user.id)):
            db.add(ApiClientUser(api_client_id=client_id, user_id=user.id))
