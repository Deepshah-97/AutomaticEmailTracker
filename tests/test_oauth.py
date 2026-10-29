"""Gmail connect: state binding, identity and domain checks, disconnect handling (A6, section 8)."""

import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.db import session_scope
from app.models import User

from .conftest import OWNER

SCOPE = "openid https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"


def id_token(**claims) -> str:
    body = {
        "aud": "cid.apps.googleusercontent.com",
        "iss": "https://accounts.google.com",
        "email": OWNER,
        "email_verified": True,
        "hd": "company.com",
    } | claims
    enc = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return f"e30.{enc}.sig"


@pytest.fixture
def oauth_env(env):
    env.tokens = {
        "refresh_token": "1//new-refresh",
        "id_token": id_token(),
        "scope": SCOPE,
    }
    env.app.state.oauth_exchange = lambda code, settings: env.tokens
    with session_scope() as db:
        u = db.get(User, env.owner_id)
        u.gmail_status, u.google_refresh_token = "never_connected", None
    return env


def start(env) -> str:
    r = env.c.get(f"/v1/users/{env.owner_id}/connect", headers=env.h())
    assert r.status_code == 200
    q = parse_qs(urlparse(r.json()["url"]).query)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"] and q["login_hint"] == [OWNER]
    assert "https://www.googleapis.com/auth/gmail.send" in q["scope"][0]
    return q["state"][0]


def test_connect_success_encrypts_token(oauth_env):
    state = start(oauth_env)
    r = oauth_env.c.get(f"/oauth/google/callback?state={state}&code=abc")
    assert r.status_code == 200 and "connected" in r.text
    with session_scope() as db:
        u = db.get(User, oauth_env.owner_id)
        assert u.gmail_status == "connected" and u.google_refresh_token.startswith("v1:")
        assert "new-refresh" not in u.google_refresh_token
        assert oauth_env.engine.cipher.decrypt(u.google_refresh_token) == "1//new-refresh"
    # State is single use.
    assert oauth_env.c.get(f"/oauth/google/callback?state={state}&code=abc").status_code == 400


def test_invalid_state(oauth_env):
    assert oauth_env.c.get("/oauth/google/callback?state=forged&code=abc").status_code == 400


@pytest.mark.parametrize(
    "claims,scope,msg",
    [
        ({"email": "someone.else@company.com"}, SCOPE, "different Google account"),
        ({"hd": "", "email": OWNER}, SCOPE, "Workspace"),
        ({"email_verified": False}, SCOPE, "not verified"),
        ({"aud": "other-client"}, SCOPE, "audience"),
        ({}, "openid", "permissions"),
    ],
)
def test_wrong_identity_or_scope_rejected(oauth_env, claims, scope, msg):
    oauth_env.tokens = {
        "refresh_token": "x",
        "id_token": id_token(**claims),
        "scope": scope,
    }
    state = start(oauth_env)
    r = oauth_env.c.get(f"/oauth/google/callback?state={state}&code=abc")
    assert r.status_code == 403 and msg.lower() in r.text.lower()
    with session_scope() as db:
        assert db.get(User, oauth_env.owner_id).gmail_status == "never_connected"


def test_invalid_grant_disconnects_and_pauses(env):
    env.seed_thread()
    t = env.create_task()
    env.box.auth_error = True
    env.c.post(f"/v1/tasks/{t['id']}/run", headers=env.h("run"))
    task = env.task(t["id"])
    assert task["paused"] and task["pause_reason"] == "gmail_disconnected"
    with session_scope() as db:
        u = db.get(User, env.owner_id)
        assert u.gmail_status == "disconnected" and u.google_refresh_token is None
    assert "user.disconnected" in env.event_names()
    assert env.c.get("/v1/users", headers=env.h()).json()["data"][0]["gmail_connected"] is False
