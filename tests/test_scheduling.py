"""Business days, send windows and scenario baselines (A4, A7)."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.engine.business_days import (
    add_business_days,
    first_check_at,
    in_send_window,
    next_send_slot,
)

NY = "America/New_York"


def ny(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(NY))


@pytest.mark.parametrize(
    "start,days,expected",
    [
        (
            ny(2026, 10, 28, 11),
            2,
            ny(2026, 10, 30, 11),
        ),  # Wed + 2 = Fri, same clock time
        (ny(2026, 10, 30, 11), 1, ny(2026, 11, 2, 11)),  # Fri + 1 = Mon
        (ny(2026, 10, 30, 11), 3, ny(2026, 11, 4, 11)),  # Fri + 3 = Wed
        (ny(2026, 10, 31, 11), 1, ny(2026, 11, 2, 11)),  # Sat + 1 = Mon
    ],
)
def test_add_business_days(start, days, expected):
    assert add_business_days(start, days, NY) == expected.astimezone(UTC)


def test_send_window_rules():
    assert next_send_slot(ny(2026, 10, 28, 7), NY) == ny(2026, 10, 28, 8, 30).astimezone(UTC)
    assert next_send_slot(ny(2026, 10, 28, 18), NY) == ny(2026, 10, 29, 8, 30).astimezone(UTC)
    assert next_send_slot(ny(2026, 10, 30, 18), NY) == ny(2026, 11, 2, 8, 30).astimezone(UTC)  # Fri evening
    assert next_send_slot(ny(2026, 10, 31, 12), NY) == ny(2026, 11, 2, 8, 30).astimezone(UTC)  # Saturday
    assert in_send_window(ny(2026, 10, 28, 17, 29), NY)
    assert not in_send_window(ny(2026, 10, 28, 17, 30), NY)


def test_time_zones_and_dst():
    # 2026-11-01 is the US DST change; Monday 08:30 New York is 13:30 UTC (EST).
    assert next_send_slot(ny(2026, 10, 31, 12), NY) == datetime(2026, 11, 2, 13, 30, tzinfo=UTC)
    tokyo = next_send_slot(datetime(2026, 10, 28, 0, 0, tzinfo=UTC), "Asia/Tokyo")
    assert tokyo == datetime(2026, 10, 28, 0, 0, tzinfo=UTC)  # 09:00 Tokyo, already in window


def test_no_show_same_day_or_next_workday():
    assert first_check_at(ny(2026, 10, 28, 11), 0, NY, "08:30", "17:30") == ny(2026, 10, 28, 11).astimezone(UTC)
    assert first_check_at(ny(2026, 10, 28, 19), 0, NY, "08:30", "17:30") == ny(2026, 10, 29, 8, 30).astimezone(UTC)
    assert first_check_at(ny(2026, 10, 30, 19), 0, NY, "08:30", "17:30") == ny(2026, 11, 2, 8, 30).astimezone(UTC)


def test_wait_lands_out_of_hours_moves_into_window():
    # Demo finished Friday 18:00; one business day later is Monday 18:00 -> Tuesday 08:30.
    assert first_check_at(ny(2026, 10, 30, 18), 1, NY, "08:30", "17:30") == ny(2026, 11, 3, 8, 30).astimezone(UTC)


@pytest.mark.parametrize(
    "scenario,extra",
    [
        ("post_demo", {}),
        ("proposal_sent", {"trigger_type": "proposal_sent_at"}),
        ("meeting_no_show", {"trigger_type": "no_show_recorded_at"}),
        (
            "custom",
            {
                "goal": "Confirm the contract signer",
                "wait_business_days": 2,
                "max_followups": 1,
                "trigger_type": "scenario_trigger_at",
            },
        ),
    ],
)
def test_scenarios_require_baseline(env, scenario, extra):
    body = {
        "external_id": f"X-{scenario}",
        "owner_email": "sam@company.com",
        "scenario": scenario,
        "client": {"name": "P", "email": "priya@acme.io"},
        "gmail_thread_id": "thr_1",
        **extra,
    }
    r = env.c.post("/v1/tasks", json=body, headers=env.h())
    assert r.status_code == 422 and r.json()["error"]["code"] == "missing_baseline"
    if "trigger_type" not in extra:
        body["trigger_type"] = "demo_completed_at"
    body["scenario_trigger_at"] = "2026-10-26T15:00:00-04:00"
    r = env.c.post("/v1/tasks", json=body, headers=env.h())
    assert r.status_code == 201, r.text


def test_custom_requires_goal_and_wait_rule(env):
    body = {
        "external_id": "C1",
        "owner_email": "sam@company.com",
        "scenario": "custom",
        "client": {"name": "P", "email": "priya@acme.io"},
        "scenario_trigger_at": "2026-10-26T15:00:00Z",
    }
    assert env.c.post("/v1/tasks", json=body, headers=env.h()).json()["error"]["code"] == "missing_goal"
    body["goal"] = "Check the trial went well"
    assert env.c.post("/v1/tasks", json=body, headers=env.h()).json()["error"]["code"] == "missing_wait_rule"


def test_naive_trigger_time_rejected(env):
    r = env.c.post(
        "/v1/tasks",
        json={
            "external_id": "N",
            "owner_email": "sam@company.com",
            "scenario": "post_demo",
            "client": {"name": "P", "email": "priya@acme.io"},
            "scenario_trigger_at": "2026-10-26T15:00:00",
        },
        headers=env.h(),
    )
    assert r.status_code == 422


def test_post_demo_scheduled_from_trigger(env):
    # Demo completed Tuesday 27 Oct 16:00 NY; wait 1 business day -> Wednesday 16:00 NY.
    t = env.create_task(scenario_trigger_at="2026-10-27T16:00:00-04:00")
    assert t["next_check_at"] == "2026-10-28T20:00:00Z"


def test_no_reply_baseline_from_thread_and_scheduled_check(env):
    # Last owner message Tuesday 27 Oct 10:00 NY; no_reply waits 2 business days -> Thu 29 Oct 10:00 NY.
    env.seed_thread(at=ny(2026, 10, 27, 7).astimezone(UTC))
    t = env.c.post(
        "/v1/tasks",
        json={
            "external_id": "NR",
            "owner_email": "sam@company.com",
            "scenario": "no_reply",
            "client": {"name": "Priya", "email": "priya@acme.io"},
            "gmail_thread_id": "thr_1",
            "notes": "SSO",
        },
        headers=env.h(),
    ).json()
    assert env.engine.check_task(t["id"]) == "scheduled"
    assert env.task(t["id"])["next_check_at"] == "2026-10-29T14:00:00Z"
    assert env.drafts() == []
    env.clock.at = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)
    assert env.engine.run_due_checks() == {"drafted": 1}


def test_run_skips_wait_only(env):
    env.seed_thread(at=env.clock() - timedelta(hours=4))
    t = env.create_task(scenario_trigger_at=env.clock().isoformat())
    assert env.engine.check_task(t["id"]) == "not_due"
    r = env.c.post(f"/v1/tasks/{t['id']}/run", headers=env.h("run"))
    assert r.status_code == 202
    assert env.task(t["id"])["status"] == "draft_pending"


def test_new_lead_uses_lead_time_and_new_thread_flow(env):
    t = env.c.post(
        "/v1/tasks",
        json={
            "external_id": "L1",
            "owner_email": "sam@company.com",
            "scenario": "new_lead",
            "client": {"name": "Lee", "email": "lee@newco.io"},
            "notes": "Inbound form: wants a demo.",
            "scenario_trigger_at": (env.clock() - timedelta(days=2)).isoformat(),
            "trigger_type": "lead_created_at",
            "allow_new_thread": True,
            "send_mode": "auto",
        },
        headers=env.h(),
    )
    assert t.status_code == 201, t.text
    t = t.json()
    assert env.engine.check_task(t["id"]) == "auto_approved"
    sent = env.box.sent_raw[0]
    assert sent["headers"]["To"] == "lee@newco.io" and "In-Reply-To" not in sent["headers"]
    assert env.task(t["id"])["gmail_thread_id"] == sent["thread_id"]


def test_new_thread_only_for_new_lead(env):
    r = env.c.post(
        "/v1/tasks",
        json={
            "external_id": "Z",
            "owner_email": "sam@company.com",
            "scenario": "no_reply",
            "client": {"name": "P", "email": "p@acme.io"},
            "allow_new_thread": True,
        },
        headers=env.h(),
    )
    assert r.status_code == 422
