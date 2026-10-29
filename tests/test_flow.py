"""End-to-end flows through the API with Gmail and Claude faked."""

from datetime import timedelta

from .conftest import CLIENT, OWNER


def run(env, task_id, idem="run-1"):
    r = env.c.post(f"/v1/tasks/{task_id}/run", headers=env.h(idem))
    assert r.status_code == 202, r.text
    return r


def test_post_demo_draft_approve_send(env):
    env.seed_thread()
    task = env.create_task()
    assert task["status"] == "waiting" and task["paused"] is False
    run(env, task["id"])
    t = env.task(task["id"])
    assert t["status"] == "draft_pending"
    drafts = env.drafts()
    assert len(drafts) == 1
    d = drafts[0]
    assert d["subject"] == "Re: Product demo"
    assert "sso" in d["body"].lower() or "reporting" in d["body"].lower()
    assert d["context_summary"] and "Thanks! Does it support SSO" not in d["context_summary"]
    assert env.event_names(task["id"]) == ["draft.ready"]

    r = env.c.post(
        f"/v1/drafts/{d['id']}/approve",
        json={"body": d["body"].replace("Thanks", "Thank you")},
        headers=env.h("appr-1"),
    )
    assert r.status_code == 202, r.text
    t = env.task(task["id"])
    assert t["status"] == "followed_up" and t["followup_count"] == 1
    assert len(env.box.sent_raw) == 1
    sent = env.box.sent_raw[0]
    assert sent["headers"]["To"] == CLIENT and sent["from"] == OWNER
    assert sent["headers"]["Subject"] == "Re: Product demo"
    assert sent["thread_id"] == "thr_1"
    last_id = env.box.threads["thr_1"][2]["payload"]["headers"][3]["value"]
    assert sent["headers"]["In-Reply-To"] == last_id
    assert last_id in sent["headers"]["References"]
    assert "Cc" not in sent["headers"] and "Bcc" not in sent["headers"]
    assert "Thank you" in sent["body"]
    names = env.event_names(task["id"])
    assert names == ["draft.ready", "followup.sent"]
    ev = env.events(task["id"])[-1]
    assert ev["task_status"] == "followed_up" and ev["data"]["sequence_number"] == 1
    assert ev["data"]["gmail_link"].endswith("thr_1")
    # The next check is scheduled (followed_up is not terminal).
    assert t["next_check_at"] is not None


def test_final_followup_exhausts_exactly_once(env):
    env.seed_thread()
    task = env.create_task(max_followups=2)
    for i in (1, 2):
        run(env, task["id"], f"run-{i}")
        d = env.drafts()[0]
        assert d["sequence_number"] == i
        env.c.post(f"/v1/drafts/{d['id']}/approve", headers=env.h(f"appr-{i}"))
        env.clock.advance(days=1)  # the per-client daily limit allows one send a day
    t = env.task(task["id"])
    assert t["status"] == "exhausted" and t["followup_count"] == 2 and t["next_check_at"] is None
    names = env.event_names(task["id"])
    assert names.count("task.exhausted") == 1
    assert names[-2:] == ["followup.sent", "task.exhausted"]
    r = env.c.post(f"/v1/tasks/{task['id']}/run", headers=env.h("run-3"))
    assert r.status_code == 409


def test_client_reply_stops_without_draft(env):
    env.seed_thread(client_replied=True)
    task = env.create_task()
    run(env, task["id"])
    t = env.task(task["id"])
    assert t["status"] == "replied"
    assert env.drafts() == [] and env.box.sent_raw == []
    assert env.event_names(task["id"]) == ["task.replied"]


def test_reply_after_draft_blocks_approval_send(env):
    env.seed_thread()
    task = env.create_task()
    run(env, task["id"])
    d = env.drafts()[0]
    env.client_reply()  # arrives after drafting, before approval
    r = env.c.post(f"/v1/drafts/{d['id']}/approve", headers=env.h("appr-1"))
    assert r.status_code == 202
    assert env.box.sent_raw == []
    t = env.task(task["id"])
    assert t["status"] == "replied"
    assert t["followups"][0]["status"] == "canceled"


def test_approval_after_reply_detected_is_refused(env):
    env.seed_thread()
    task = env.create_task()
    run(env, task["id"])
    d = env.drafts()[0]
    env.engine.mark_replied(task["id"], "client_replied")
    r = env.c.post(f"/v1/drafts/{d['id']}/approve", headers=env.h("appr-1"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "task_closed"
    assert env.box.sent_raw == []


def test_duplicate_approval_sends_once(env):
    env.seed_thread()
    task = env.create_task()
    run(env, task["id"])
    d = env.drafts()[0]
    r1 = env.c.post(f"/v1/drafts/{d['id']}/approve", headers=env.h("same-key"))
    r2 = env.c.post(f"/v1/drafts/{d['id']}/approve", headers=env.h("same-key"))
    assert r1.status_code == r2.status_code == 202
    assert r2.headers.get("Idempotent-Replayed") == "true"
    r3 = env.c.post(f"/v1/drafts/{d['id']}/approve", headers=env.h("other-key"))
    assert r3.status_code == 409
    assert len(env.box.sent_raw) == 1
    assert env.event_names(task["id"]).count("followup.sent") == 1


def test_reject_pauses_then_resume_redrafts_same_record(env):
    env.seed_thread()
    task = env.create_task()
    run(env, task["id"])
    d = env.drafts()[0]
    r = env.c.post(
        f"/v1/drafts/{d['id']}/reject",
        json={"feedback": "Mention the pricing sheet"},
        headers=env.h("rej-1"),
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    t = env.task(task["id"])
    assert t["paused"] and t["pause_reason"] == "draft_rejected" and t["status"] == "waiting"
    assert t["followup_count"] == 0
    assert env.event_names(task["id"])[-1] == "draft.rejected"
    assert env.events(task["id"])[-1]["paused"] is True
    r = env.c.post(f"/v1/tasks/{task['id']}/run", headers=env.h("run-2"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "paused"

    env.c.post(f"/v1/tasks/{task['id']}/resume", headers=env.h("res-1"))
    d2 = env.drafts()[0]
    assert d2["id"] == d["id"] and d2["sequence_number"] == 1  # same sequence record
    t = env.task(task["id"])
    assert t["followups"][0]["draft_revision"] == 2
    assert any(e["type"] == "draft.revision_archived" for e in t["timeline"])


def test_auto_mode_sends_without_approval(env):
    env.seed_thread()
    task = env.create_task(send_mode="auto")
    run(env, task["id"])
    assert len(env.box.sent_raw) == 1
    assert env.event_names(task["id"]) == ["followup.sent"]


def test_needs_review_forces_approval_even_in_auto(env):
    env.seed_thread()
    task = env.create_task(send_mode="auto", notes=None)
    run(env, task["id"])  # FakeDrafter sets needs_review when notes are empty
    assert env.box.sent_raw == []
    assert env.drafts()[0]["needs_review"] is True


def test_preview_does_not_create_sendable_draft(env):
    env.seed_thread()
    task = env.create_task()
    r = env.c.post(f"/v1/tasks/{task['id']}/preview", headers=env.h())
    assert r.status_code == 200 and r.json()["subject"] == "Re: Product demo"
    assert env.drafts() == []
    assert env.task(task["id"])["followups"] == []


def test_external_meeting_booked_stops_queued_send(env):
    env.seed_thread()
    task = env.create_task()
    run(env, task["id"])
    d = env.drafts()[0]
    # Approve outside the window so it is queued, then a meeting is booked.
    env.clock.at = env.clock.at.replace(hour=23)
    env.c.post(f"/v1/drafts/{d['id']}/approve", headers=env.h("appr-1"))
    assert env.box.sent_raw == []
    body = {
        "type": "meeting_booked",
        "occurred_at": env.clock().isoformat(),
        "source_event_id": "cal-123",
    }
    r1 = env.c.post(f"/v1/tasks/{task['id']}/events", json=body, headers=env.h())
    r2 = env.c.post(f"/v1/tasks/{task['id']}/events", json=body, headers=env.h())
    assert r1.status_code == 200 and r2.status_code == 200
    env.clock.advance(hours=12)
    env.engine.send_due()
    assert env.box.sent_raw == []
    t = env.task(task["id"])
    assert t["status"] == "stopped" and t["stop_reason"] == "meeting_booked"
    assert env.event_names(task["id"]).count("task.stopped") == 1


def test_opt_out_reply_stops(env):
    env.seed_thread()
    env.client_reply(
        body="Please remove me from your list, not interested.",
        at=env.clock() - timedelta(hours=1),
    )
    task = env.create_task()
    run(env, task["id"])
    t = env.task(task["id"])
    assert t["status"] == "stopped" and t["stop_reason"] == "opted_out"


def test_owner_from_other_client_is_forbidden(env):
    r = env.c.post(
        "/v1/tasks",
        json={
            "external_id": "X",
            "owner_email": "someone@company.com",
            "scenario": "no_reply",
            "client": {"name": "A", "email": CLIENT},
            "gmail_thread_id": "thr_1",
        },
        headers=env.h(),
    )
    assert r.status_code == 403


def test_owner_email_constant():
    assert OWNER.endswith("@company.com")
