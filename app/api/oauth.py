"""Google OAuth callback: validates state, identity and domain before storing an encrypted token (A6)."""

import base64
import hashlib
import json
from html import escape
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..core import webhooks
from ..db import session_scope
from ..models import OAuthState, Task, User, utcnow

router = APIRouter(tags=["users"])
REQUIRED_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
}


def google_exchange(code: str, settings) -> dict[str, Any]:
    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.oauth_redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise ValueError(f"token exchange failed ({resp.status_code})")
    return resp.json()


def _claims(id_token: str) -> dict[str, Any]:
    # The ID token comes straight from Google's token endpoint over TLS, so its claims can be read
    # without re-verifying the signature (OpenID Connect Core 3.1.3.7). aud/iss are still checked.
    payload = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _page(title: str, message: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title>"
        f"<body style='font-family:sans-serif;max-width:32rem;margin:4rem auto'>"
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p></body>",
        status_code=status,
    )


@router.get("/oauth/google/callback", response_class=HTMLResponse)
def callback(request: Request, state: str = "", code: str = "", error: str = "") -> HTMLResponse:
    settings = request.app.state.settings
    engine = request.app.state.engine
    if error:
        return _page("Gmail not connected", f"Google returned: {error}", 400)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    with session_scope() as db:
        row = db.get(OAuthState, state_hash, with_for_update=True)
        if row is None or row.used_at is not None or row.expires_at < utcnow():
            return _page(
                "Link expired",
                "This sign-in link is invalid or expired. Start again from the dashboard.",
                400,
            )
        row.used_at = utcnow()
        user_id = row.user_id
    try:
        tokens = request.app.state.oauth_exchange(code, settings)
        claims = _claims(tokens.get("id_token", ""))
    except Exception:
        return _page(
            "Gmail not connected",
            "Google sign-in could not be completed. Try again.",
            400,
        )
    granted = set((tokens.get("scope") or "").split())
    with session_scope() as db:
        user = db.get(User, user_id, with_for_update=True)
        email = str(claims.get("email", "")).lower()
        domain = email.rsplit("@", 1)[-1]
        problems = []
        if claims.get("aud") != settings.google_client_id:
            problems.append("token audience mismatch")
        if claims.get("iss") not in (
            "https://accounts.google.com",
            "accounts.google.com",
        ):
            problems.append("unexpected issuer")
        if not claims.get("email_verified"):
            problems.append("email not verified")
        if user is None or email != user.email.lower():
            problems.append("signed in with a different Google account than the provisioned team member")
        if settings.allowed_domains and (
            domain not in settings.allowed_domains or claims.get("hd", "").lower() not in settings.allowed_domains
        ):
            problems.append("account is not in the company's Google Workspace")
        if not REQUIRED_SCOPES <= granted:
            problems.append("Gmail read and send permissions were not granted")
        if not tokens.get("refresh_token"):
            problems.append("no refresh token returned")
        if problems:
            if user:
                webhooks.audit(db, "oauth.rejected", None, "; ".join(problems), user_id=user_id)
            return _page("Gmail not connected", "; ".join(problems).capitalize() + ".", 403)
        user.google_refresh_token = engine.cipher.encrypt(tokens["refresh_token"])
        user.gmail_status = "connected"
        for task in db.query(Task).filter(Task.owner_user_id == user.id, Task.pause_reason == "gmail_disconnected"):
            task.paused, task.pause_reason = False, None
        webhooks.audit(db, "user.connected", None, user.email, user_id=user.id)
    return _page("Gmail connected", f"{email} is connected. You can close this tab.", 200)
