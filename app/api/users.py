"""Team members, Gmail connect and the thread picker."""

import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import idempotency
from ..core.auth import Caller, require_client, scoped_user
from ..core.errors import ApiError
from ..db import get_db, session_scope
from ..engine.check import FollowupEngine
from ..gmail.client import SCOPES, GmailAuthError, GmailUnavailable
from ..gmail.thread import summarize
from ..models import OAuthState, User, utcnow
from ..schemas import UserUpdate, user_out
from .deps import engine, idem_key

router = APIRouter(prefix="/v1/users", tags=["users"])


@router.get("")
def list_users(caller: Caller = Depends(require_client), db: Session = Depends(get_db)) -> dict:
    users = db.scalars(select(User).where(User.id.in_(caller.user_ids), User.active.is_(True)).order_by(User.name))
    return {"data": [user_out(u) for u in users]}


@router.patch("/{user_id}")
def update_user(
    user_id: str,
    body: UserUpdate,
    request: Request,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    def do():
        user = scoped_user(db, caller, user_id)
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(user, k, v)
        return 200, user_out(user)

    return idempotency.run(
        db,
        caller.client.id,
        key,
        "PATCH",
        request.url.path,
        body.model_dump(mode="json", exclude_unset=True),
        do,
    )


@router.get("/{user_id}/connect")
def connect(
    user_id: str,
    request: Request,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
) -> dict:
    """Sign-in URL bound to this provisioned user with a single-use, expiring state (A6)."""
    settings = request.app.state.settings
    user = scoped_user(db, caller, user_id)
    if not settings.google_client_id:
        raise ApiError(503, "oauth_not_configured", "Google OAuth is not configured on the agent")
    state = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(minutes=10)
    db.add(
        OAuthState(
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
            user_id=user.id,
            api_client_id=caller.client.id,
            expires_at=expires,
        )
    )
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
        "login_hint": user.email,
    }
    domains = settings.allowed_domains
    if len(domains) == 1:
        params["hd"] = next(iter(domains))
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    return {"url": url, "expires_at": expires.isoformat().replace("+00:00", "Z")}


@router.get("/{user_id}/threads")
def threads(
    user_id: str,
    q: str = "",
    limit: int = 10,
    caller: Caller = Depends(require_client),
    eng: FollowupEngine = Depends(engine),
) -> dict:
    with session_scope() as db:
        user = scoped_user(db, caller, user_id)
        if user.gmail_status != "connected":
            raise ApiError(409, "gmail_disconnected", "Connect Gmail first")
        token = user.google_refresh_token
    try:
        found = eng.gmail_for(token).search_threads(q or "in:anywhere", max(1, min(limit, 25)))
    except GmailAuthError:
        eng.disconnect_user(user_id, "invalid_grant")
        raise ApiError(409, "gmail_disconnected", "Gmail access was revoked; reconnect") from None
    except GmailUnavailable:
        raise ApiError(503, "gmail_unavailable", "Gmail could not be reached") from None
    return {"data": [summarize(t) for t in found]}
