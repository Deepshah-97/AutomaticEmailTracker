"""Auth, scoping, idempotency, validation and error format (A5, section 9)."""

from .conftest import CLIENT, OWNER

ROUTES = [
    ("GET", "/v1/tasks"),
    ("POST", "/v1/tasks"),
    ("GET", "/v1/tasks/x"),
    ("PATCH", "/v1/tasks/x"),
    ("POST", "/v1/tasks/x/pause"),
    ("POST", "/v1/tasks/x/resume"),
    ("POST", "/v1/tasks/x/stop"),
    ("POST", "/v1/tasks/x/preview"),
    ("POST", "/v1/tasks/x/run"),
    ("POST", "/v1/tasks/x/events"),
    ("GET", "/v1/drafts"),
    ("POST", "/v1/drafts/x/approve"),
    ("POST", "/v1/drafts/x/reject"),
    ("GET", "/v1/users"),
    ("PATCH", "/v1/users/x"),
    ("GET", "/v1/users/x/connect"),
    ("GET", "/v1/users/x/threads"),
]


def test_health_is_public(env):
    assert env.c.get("/v1/health").json()["status"] == "ok"


def test_revoked_key_gets_401_everywhere(env):
    env.admin("POST", f"/v1/admin/api-clients/{env.client_id}/revoke")
    for method, path in ROUTES:
        r = env.c.request(method, path, headers=env.h(), json={})
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.json()["error"]["code"] == "unauthorized"


def test_missing_and_wrong_keys(env):
    assert env.c.get("/v1/tasks").status_code == 401
    assert env.c.get("/v1/tasks", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert env.c.post("/v1/admin/pause-all", headers=env.h()).status_code == 401  # client key is not admin


def test_rate_limit(make_env):
    env = make_env(rate_limit_per_minute=5)
    codes = [env.c.get("/v1/tasks", headers=env.h()).status_code for _ in range(6)]
    assert codes[-1] == 429 and codes[:5] == [200] * 5


def test_error_format(env):
    r = env.c.get("/v1/tasks/nope", headers=env.h())
    assert r.status_code == 404 and set(r.json()["error"]) == {"code", "message"}
    r = env.c.post("/v1/tasks", json={"external_id": 1}, headers=env.h())
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_create_idempotent_and_key_reuse_detected(env):
    t1 = env.create_task(idem="create-1")
    t2 = env.create_task(idem="create-1")
    assert t1["id"] == t2["id"]
    r = env.c.post(
        "/v1/tasks",
        json={
            "external_id": "OTHER",
            "owner_email": OWNER,
            "scenario": "no_reply",
            "client": {"name": "P", "email": CLIENT},
            "gmail_thread_id": "thr_1",
        },
        headers=env.h("create-1"),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_reused"
    r = env.c.post(
        "/v1/tasks",
        json={
            "external_id": "TRK-1042",
            "owner_email": OWNER,
            "scenario": "no_reply",
            "client": {"name": "P", "email": CLIENT},
            "gmail_thread_id": "thr_1",
        },
        headers=env.h(),
    )
    assert r.status_code == 409


def test_other_client_cannot_see_or_touch_tasks(env):
    t = env.create_task()
    other = env.admin("POST", "/v1/admin/api-clients", json={"name": "Other", "user_emails": [OWNER]}).json()
    h = {"Authorization": f"Bearer {other['api_key']}"}
    assert env.c.get(f"/v1/tasks/{t['id']}", headers=h).status_code == 404
    assert env.c.post(f"/v1/tasks/{t['id']}/run", headers=h).status_code == 404
    assert env.c.get("/v1/tasks", headers=h).json()["data"] == []


def test_client_without_user_grant_cannot_use_mailbox(env):
    other = env.admin("POST", "/v1/admin/api-clients", json={"name": "Unscoped"}).json()
    h = {"Authorization": f"Bearer {other['api_key']}"}
    assert env.c.get("/v1/users", headers=h).json()["data"] == []
    assert env.c.get(f"/v1/users/{env.owner_id}/connect", headers=h).status_code == 403
    assert env.c.patch(f"/v1/users/{env.owner_id}", json={"paused": True}, headers=h).status_code == 403
    r = env.c.post(
        "/v1/tasks",
        json={
            "external_id": "A",
            "owner_email": OWNER,
            "scenario": "no_reply",
            "client": {"name": "P", "email": CLIENT},
            "gmail_thread_id": "t",
        },
        headers=h,
    )
    assert r.status_code == 403


def test_list_fields_filters_and_pagination(env):
    for i in range(3):
        env.create_task(external_id=f"T{i}")
    page = env.c.get("/v1/tasks?limit=2", headers=env.h()).json()
    assert len(page["data"]) == 2 and page["next_cursor"]
    rest = env.c.get(f"/v1/tasks?limit=2&cursor={page['next_cursor']}", headers=env.h()).json()
    assert len(rest["data"]) == 1 and rest["next_cursor"] is None
    t = page["data"][0]
    for field in [
        "id",
        "external_id",
        "owner_user_id",
        "owner_email",
        "scenario",
        "status",
        "paused",
        "followup_count",
        "max_followups",
        "next_check_at",
        "created_at",
        "updated_at",
    ]:
        assert field in t
    assert env.c.get("/v1/tasks?status=replied", headers=env.h()).json()["data"] == []
    assert len(env.c.get(f"/v1/tasks?owner_email={OWNER}", headers=env.h()).json()["data"]) == 3


def test_users_endpoint_shape(env):
    u = env.c.get("/v1/users", headers=env.h()).json()["data"][0]
    assert u["gmail_connected"] is True and u["send_mode"] == "approve" and u["paused"] is False
    r = env.c.patch(
        f"/v1/users/{env.owner_id}",
        json={"send_mode": "auto", "timezone": "Europe/London"},
        headers=env.h(),
    )
    assert r.json()["send_mode"] == "auto" and r.json()["timezone"] == "Europe/London"
    assert env.c.patch(f"/v1/users/{env.owner_id}", json={"timezone": "Mars/Base"}, headers=env.h()).status_code == 422


def test_thread_picker(env):
    env.seed_thread()
    data = env.c.get(f"/v1/users/{env.owner_id}/threads?q=from:{CLIENT}", headers=env.h()).json()["data"]
    assert data[0]["thread_id"] == "thr_1" and data[0]["subject"] == "Product demo"


def test_pause_resume_stop(env):
    t = env.create_task()
    assert env.c.post(f"/v1/tasks/{t['id']}/pause", headers=env.h()).json()["paused"] is True
    assert env.c.post(f"/v1/tasks/{t['id']}/resume", headers=env.h()).json()["paused"] is False
    r = env.c.post(f"/v1/tasks/{t['id']}/stop", json={"reason": "Deal closed"}, headers=env.h())
    assert r.json()["status"] == "stopped" and r.json()["stop_reason"] == "Deal closed"
    assert env.event_names(t["id"]) == ["task.stopped"]
    assert env.c.post(f"/v1/tasks/{t['id']}/resume", headers=env.h()).status_code == 409


def test_export_has_no_secrets(env):
    env.create_task()
    data = env.admin("GET", "/v1/admin/export").text
    assert "refresh-" not in data and "google_refresh_token" not in data and env.webhook_secret not in data
    assert "TRK-1042" in data
