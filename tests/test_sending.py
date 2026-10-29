"""Send rules, limits, TEST_MODE, idempotent sending and crash recovery (A2, A7, A8)."""

import threading
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import session_scope
from app.models import Followup

from .conftest import CLIENT


def drafted(env, **kw):
    env.seed_thread()
    t = env.create_task(**kw)
    env.c.post(f"/v1/tasks/{t['id']}/run", headers=env.h("run"))
    return t, env.drafts()[0]


def approve(env, d, key="appr"):
    return env.c.post(f"/v1/drafts/{d['id']}/approve", headers=env.h(key))


def test_out_of_hours_approval_is_queued(env):
    t, d = drafted(env)
    env.clock.at = datetime(2026, 10, 28, 23, 30, tzinfo=UTC)  # 19:30 New York
    r = approve(env, d)
    assert r.json()["scheduled_send_at"] == "2026-10-29T12:30:00Z"  # Thu 08:30 New York
    assert env.box.sent_raw == []
    env.clock.at = datetime(2026, 10, 29, 12, 31, tzinfo=UTC)
    assert env.engine.send_due() == {"sent": 1}


def test_weekend_send_moves_to_monday(env):
    t, d = drafted(env)
    env.clock.at = datetime(2026, 10, 31, 16, 0, tzinfo=UTC)  # Saturday
    assert approve(env, d).json()["scheduled_send_at"] == "2026-11-02T13:30:00Z"


def test_one_send_per_client_per_day(env):
    t, d = drafted(env, max_followups=3)
    approve(env, d)
    env.c.post(f"/v1/tasks/{t['id']}/run", headers=env.h("run2"))
    d2 = env.drafts()[0]
    approve(env, d2, "appr2")
    assert len(env.box.sent_raw) == 1
    f = env.task(t["id"])["followups"][1]
    assert f["status"] == "approved" and f["scheduled_send_at"] == "2026-10-29T12:30:00Z"


def test_sending_disabled_holds_everything(make_env):
    env = make_env(sending_enabled=False)
    t, d = drafted(env)
    approve(env, d)
    assert env.box.sent_raw == []
    assert env.task(t["id"])["followups"][0]["status"] == "approved"


def test_test_mode_allowlisted_skips_window_and_delay(make_env):
    env = make_env(
        test_mode=True,
        test_allowlist=f"sam@company.com,{CLIENT}",
        sending_enabled=False,
        random_delay_max_minutes=40,
    )
    t, d = drafted(env)
    env.clock.at = datetime(2026, 10, 31, 3, 0, tzinfo=UTC)  # Saturday night
    approve(env, d)
    assert len(env.box.sent_raw) == 1


def test_test_mode_blocks_non_allowlisted_recipient(make_env):
    env = make_env(test_mode=True, test_allowlist="sam@company.com,other@acme.io")
    t, d = drafted(env)
    approve(env, d)
    assert env.box.sent_raw == []
    task = env.task(t["id"])
    assert task["pause_reason"] == "not_allowlisted"
    assert task["followups"][0]["status"] == "failed"


def test_test_mode_still_requires_approval_and_reply_check(make_env):
    env = make_env(test_mode=True, test_allowlist=f"sam@company.com,{CLIENT}")
    env.seed_thread(client_replied=True)
    t = env.create_task()
    env.c.post(f"/v1/tasks/{t['id']}/run", headers=env.h("run"))
    assert env.task(t["id"])["status"] == "replied" and env.box.sent_raw == []


def test_concurrent_send_workers_send_once(env):
    t, d = drafted(env)
    with session_scope() as db:
        f = db.get(Followup, d["id"])
        f.status, f.scheduled_send_at = "approved", env.clock()
    results = []
    workers = [threading.Thread(target=lambda: results.append(env.engine.send_followup(d["id"]))) for _ in range(4)]
    [w.start() for w in workers]
    [w.join() for w in workers]
    assert results.count("sent") >= 1
    assert len(env.box.sent_raw) == 1
    assert env.task(t["id"])["followup_count"] == 1


def test_concurrent_checks_create_one_draft(env):
    env.seed_thread()
    t = env.create_task()
    results = []
    workers = [
        threading.Thread(target=lambda: results.append(env.engine.check_task(t["id"], force=True))) for _ in range(4)
    ]
    [w.start() for w in workers]
    [w.join() for w in workers]
    assert results.count("drafted") == 1
    with session_scope() as db:
        assert len(list(db.scalars(select(Followup)))) == 1


def test_timeout_after_accept_reconciles_to_sent(env):
    t, d = drafted(env)
    env.box.send_behavior = "timeout_after_accept"
    approve(env, d)
    task = env.task(t["id"])
    assert task["followups"][0]["status"] == "sent" and task["followup_count"] == 1
    assert len(env.box.sent_raw) == 1


def test_timeout_without_delivery_is_delivery_unknown_and_never_resent(env):
    t, d = drafted(env)
    env.box.send_behavior = "timeout_before_accept"
    approve(env, d)
    task = env.task(t["id"])
    assert task["followups"][0]["status"] == "delivery_unknown"
    assert task["paused"] and task["pause_reason"] == "delivery_unknown"
    assert env.event_names(t["id"])[-1] == "task.error"
    env.box.send_behavior = "ok"
    env.engine.send_due()
    env.engine.recover_stale_sends()
    assert env.box.sent_raw == []
    assert task["followup_count"] == 0


def test_crash_mid_send_recovered_by_message_id(env):
    t, d = drafted(env)
    # Simulate: claimed and sent by Gmail, then the process died before recording it.
    approve_time = env.clock()
    with session_scope() as db:
        f = db.get(Followup, d["id"])
        f.status, f.outbound_message_id, f.claimed_until = (
            "sending",
            "<fua.crash@company.com>",
            approve_time,
        )
    env.box.add(
        "thr_1",
        sender="sam@company.com",
        to=CLIENT,
        subject="Re: Product demo",
        body="sent",
        at=approve_time,
        message_id="<fua.crash@company.com>",
    )
    env.clock.advance(minutes=10)
    assert env.engine.recover_stale_sends() == 1
    task = env.task(t["id"])
    assert task["followups"][0]["status"] == "sent" and task["followup_count"] == 1
    assert env.event_names(t["id"]).count("followup.sent") == 1


def test_gmail_rejection_marks_failed(env):
    t, d = drafted(env)
    env.box.send_behavior = "rejected"
    approve(env, d)
    task = env.task(t["id"])
    assert task["followups"][0]["status"] == "failed" and task["pause_reason"] == "send_failed"


def test_edited_draft_failing_checks_is_refused(env):
    t, d = drafted(env)
    for body, msg in [
        ("Hi [Name], see you", "placeholder"),
        ("Hi Priya, it's $999 for you.", "price"),
        ("Hi Priya, this automated email is for you.", "AI"),
        ("word " * 151, "150"),
    ]:
        r = env.c.post(f"/v1/drafts/{d['id']}/approve", json={"body": body}, headers=env.h())
        assert r.status_code == 422, body
        assert msg in r.json()["error"]["message"]
    assert env.box.sent_raw == []
    # A price that is in the notes is allowed.
    env.c.patch(
        f"/v1/tasks/{t['id']}",
        json={"notes": "Quoted $1,200 for 40 seats."},
        headers=env.h(),
    )
    r = env.c.post(
        f"/v1/drafts/{d['id']}/approve",
        json={"body": "Hi Priya, as discussed it is $1,200 for 40 seats."},
        headers=env.h(),
    )
    assert r.status_code == 202, r.text


def test_edits_feed_style_examples(env):
    t, d = drafted(env)
    env.c.post(
        f"/v1/drafts/{d['id']}/approve",
        json={"body": d["body"].replace("Thanks", "Thank you")},
        headers=env.h(),
    )
    from app.models import User

    with session_scope() as db:
        assert any("Thank you" in e for e in db.get(User, env.owner_id).style_examples)


def test_pause_all_blocks_sends(env):
    t, d = drafted(env)
    env.admin("POST", "/v1/admin/pause-all", json={"paused": True})
    approve(env, d)
    assert env.box.sent_raw == []
    env.admin("POST", "/v1/admin/pause-all", json={"paused": False})
    env.engine.send_due()
    assert len(env.box.sent_raw) == 1


def test_owner_paused_blocks_checks(env):
    env.seed_thread()
    t = env.create_task()
    env.c.patch(f"/v1/users/{env.owner_id}", json={"paused": True}, headers=env.h())
    r = env.c.post(f"/v1/tasks/{t['id']}/run", headers=env.h("r"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "owner_paused"
