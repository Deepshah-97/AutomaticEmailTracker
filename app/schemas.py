"""Pydantic request models and response serializers (see openapi.yaml)."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .gmail.send import gmail_link
from .models import Event, Followup, Task, User

Scenario = Literal["no_reply", "new_lead", "post_demo", "proposal_sent", "meeting_no_show", "custom"]
SendMode = Literal["approve", "auto"]

# Scenario defaults: (wait in business days, max follow-ups). custom has no default.
SCENARIO_DEFAULTS: dict[str, tuple[int, int] | None] = {
    "no_reply": (2, 3),
    "new_lead": (1, 3),
    "post_demo": (1, 2),
    "proposal_sent": (3, 3),
    "meeting_no_show": (0, 2),
    "custom": None,
}
REQUIRES_TRIGGER = {"post_demo", "proposal_sent", "meeting_no_show", "custom"}
TRIGGER_TYPES = {
    "new_lead": {"lead_created_at", "received_at", "scenario_trigger_at"},
    "post_demo": {"demo_completed_at", "scenario_trigger_at"},
    "proposal_sent": {"proposal_sent_at", "scenario_trigger_at"},
    "meeting_no_show": {"no_show_recorded_at", "scenario_trigger_at"},
    "custom": {"scenario_trigger_at"},
}


def valid_timezone(name: str) -> str:
    """Raise ValueError (a 422) for anything that is not an IANA zone name."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, ModuleNotFoundError) as e:
        raise ValueError("must be an IANA time zone, e.g. America/New_York") from e
    return name


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    company: str | None = Field(default=None, max_length=200)


class TaskCreate(Strict):
    external_id: str = Field(min_length=1, max_length=100)
    owner_email: EmailStr
    scenario: Scenario
    client: ClientIn
    goal: str | None = Field(default=None, max_length=1000)
    gmail_thread_id: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=10000)
    wait_business_days: int | None = Field(default=None, ge=0, le=30)
    max_followups: int | None = Field(default=None, ge=1, le=5)
    send_mode: SendMode | None = None
    scenario_trigger_at: datetime | None = None
    trigger_type: str | None = None
    paused: bool = False
    allow_new_thread: bool = False

    @field_validator("scenario_trigger_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("must include a time zone offset")
        return v


class TaskUpdate(Strict):
    notes: str | None = Field(default=None, max_length=10000)
    goal: str | None = Field(default=None, max_length=1000)
    wait_business_days: int | None = Field(default=None, ge=0, le=30)
    max_followups: int | None = Field(default=None, ge=1, le=5)
    send_mode: SendMode | None = None
    gmail_thread_id: str | None = Field(default=None, max_length=100)


class StopIn(Strict):
    reason: str | None = Field(default=None, max_length=500)


class ExternalEventIn(Strict):
    type: Literal["meeting_booked", "task_closed", "opted_out"]
    occurred_at: datetime
    source_event_id: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=500)


class ApproveIn(Strict):
    subject: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=5000)


class RejectIn(Strict):
    feedback: str | None = Field(default=None, max_length=2000)


class UserUpdate(Strict):
    signature: str | None = Field(default=None, max_length=1000)
    style_examples: list[str] | None = Field(default=None, max_length=10)
    timezone: str | None = None
    send_mode: SendMode | None = None
    paused: bool | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return valid_timezone(v) if v is not None else v


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def task_out(task: Task, owner_email: str) -> dict[str, Any]:
    return {
        "id": task.id,
        "external_id": task.external_id,
        "owner_user_id": task.owner_user_id,
        "owner_email": owner_email,
        "scenario": task.scenario,
        "status": task.status,
        "paused": task.paused,
        "pause_reason": task.pause_reason,
        "client": {
            "name": task.client_name,
            "email": task.client_email,
            "company": task.client_company,
        },
        "gmail_thread_id": task.gmail_thread_id,
        "followup_count": task.followup_count,
        "max_followups": task.max_followups,
        "wait_business_days": task.wait_business_days,
        "send_mode": task.send_mode,
        "scenario_trigger_at": iso(task.scenario_trigger_at),
        "next_check_at": iso(task.next_check_at),
        "last_outbound_at": iso(task.last_outbound_at),
        "last_inbound_at": iso(task.last_inbound_at),
        "stop_reason": task.stop_reason,
        "error_message": task.error_message,
        "created_at": iso(task.created_at),
        "updated_at": iso(task.updated_at),
    }


def followup_out(f: Followup) -> dict[str, Any]:
    return {
        "id": f.id,
        "task_id": f.task_id,
        "sequence_number": f.sequence_number,
        "draft_revision": f.draft_revision,
        "status": f.status,
        "subject": f.subject,
        "body": f.body,
        "needs_review": f.needs_review,
        "reason": f.reason,
        "edited": f.edited,
        "gmail_message_id": f.gmail_message_id,
        "scheduled_send_at": iso(f.scheduled_send_at),
        "created_at": iso(f.created_at),
        "sent_at": iso(f.sent_at),
    }


def task_detail_out(task: Task, owner_email: str, followups: list[Followup], events: list[Event]) -> dict[str, Any]:
    out = task_out(task, owner_email)
    out.update(
        {
            "notes": task.notes,
            "goal": task.goal,
            "gmail_thread_url": gmail_link(task.gmail_thread_id),
            "followups": [followup_out(f) for f in followups],
            "timeline": [
                {
                    "at": iso(e.created_at),
                    "type": e.event,
                    "detail": (e.payload or {}).get("detail") or _event_detail(e),
                }
                for e in events
            ],
        }
    )
    return out


def _event_detail(e: Event) -> str | None:
    data = (e.payload or {}).get("data") or {}
    for key in ("reason", "message", "code", "subject"):
        if data.get(key):
            return str(data[key])[:200]
    return None


def draft_out(f: Followup, task: Task, owner_email: str) -> dict[str, Any]:
    return {
        "id": f.id,
        "task_id": task.id,
        "external_id": task.external_id,
        "sequence_number": f.sequence_number,
        "subject": f.subject,
        "body": f.body,
        "needs_review": f.needs_review,
        "reason": f.reason,
        "context_summary": f.context_summary,
        "client": {
            "name": task.client_name,
            "email": task.client_email,
            "company": task.client_company,
        },
        "owner_email": owner_email,
        "created_at": iso(f.created_at),
    }


def user_out(u: User) -> dict[str, Any]:
    return {
        "id": u.id,
        "email": u.email,
        "name": u.name,
        "timezone": u.timezone,
        "send_mode": u.send_mode,
        "paused": u.paused,
        "gmail_connected": u.gmail_status == "connected",
        "gmail_status": u.gmail_status,
    }
