"""ORM tables.

The five spec tables (users, tasks, followups, api_clients, events) plus the small supporting
tables the v1.1 amendments need: API-client scoping, stored idempotent responses, OAuth state
and a key/value table for the global pause switch.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True), dict[str, Any]: JSONType}


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("usr"))
    email: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String, default="America/New_York")
    signature: Mapped[str] = mapped_column(Text, default="")
    style_examples: Mapped[list[str]] = mapped_column(JSONType, default=list)
    google_refresh_token: Mapped[str | None] = mapped_column(Text)  # AES-GCM ciphertext
    gmail_status: Mapped[str] = mapped_column(String, default="never_connected")
    send_mode: Mapped[str] = mapped_column(String, default="approve")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ApiClient(Base):
    __tablename__ = "api_clients"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("cli"))
    name: Mapped[str] = mapped_column(String)
    api_key_hash: Mapped[str] = mapped_column(String, unique=True)
    webhook_url: Mapped[str | None] = mapped_column(String)
    webhook_secret: Mapped[str] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ApiClientUser(Base):
    """Which team members an API client may act for (A5: a key alone is not access to every mailbox)."""

    __tablename__ = "api_client_users"

    api_client_id: Mapped[str] = mapped_column(ForeignKey("api_clients.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("api_client_id", "external_id"),
        Index("ix_tasks_due", "status", "paused", "next_check_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("tsk"))
    external_id: Mapped[str] = mapped_column(String)
    api_client_id: Mapped[str] = mapped_column(ForeignKey("api_clients.id"))
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    scenario: Mapped[str] = mapped_column(String)
    goal: Mapped[str | None] = mapped_column(Text)
    client_name: Mapped[str] = mapped_column(String)
    client_email: Mapped[str] = mapped_column(String, index=True)
    client_company: Mapped[str | None] = mapped_column(String)
    gmail_thread_id: Mapped[str | None] = mapped_column(String)
    allow_new_thread: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="waiting")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str | None] = mapped_column(String)
    followup_count: Mapped[int] = mapped_column(Integer, default=0)
    max_followups: Mapped[int] = mapped_column(Integer)
    wait_business_days: Mapped[int] = mapped_column(Integer)
    scenario_trigger_at: Mapped[datetime | None]
    trigger_type: Mapped[str | None] = mapped_column(String)
    last_outbound_at: Mapped[datetime | None]
    last_inbound_at: Mapped[datetime | None]
    next_check_at: Mapped[datetime | None]
    send_mode: Mapped[str | None] = mapped_column(String)
    stop_reason: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(Text)
    review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    # Lease so two workers never check the same task concurrently (claimed in a short transaction).
    claimed_until: Mapped[datetime | None]
    claimed_by: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Followup(Base):
    """One record per (task, sequence) for its whole lifecycle (A2)."""

    __tablename__ = "followups"
    __table_args__ = (UniqueConstraint("task_id", "sequence_number"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("fup"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    sequence_number: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String, unique=True)  # task_id:sequence_number
    draft_revision: Mapped[int] = mapped_column(Integer, default=1)
    # drafted, approved, sending, sent, rejected, failed, delivery_unknown, canceled
    status: Mapped[str] = mapped_column(String, default="drafted")
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    original_body: Mapped[str | None] = mapped_column(Text)  # AI text before any human edit
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    context_summary: Mapped[str | None] = mapped_column(Text)
    safety_failures: Mapped[list[str]] = mapped_column(JSONType, default=list)
    # Prices/dates found in the thread and notes at draft time, to validate human edits without
    # storing the thread itself.
    allowed_facts: Mapped[list[str]] = mapped_column(JSONType, default=list)
    thread_subject: Mapped[str | None] = mapped_column(Text)
    recipient: Mapped[str] = mapped_column(String, default="")
    gmail_thread_id: Mapped[str | None] = mapped_column(String)
    in_reply_to: Mapped[str | None] = mapped_column(String)
    references: Mapped[str | None] = mapped_column(Text)
    outbound_attempt_id: Mapped[str | None] = mapped_column(String, unique=True)
    outbound_message_id: Mapped[str | None] = mapped_column(String, unique=True)  # RFC 5322 Message-ID
    gmail_message_id: Mapped[str | None] = mapped_column(String)
    scheduled_send_at: Mapped[datetime | None]
    claimed_until: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)
    feedback: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    approved_at: Mapped[datetime | None]
    sending_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    rejected_at: Mapped[datetime | None]
    failed_at: Mapped[datetime | None]
    canceled_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Event(Base):
    """Append-only audit log and webhook outbox (A5)."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_outbox", "delivery_status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String, unique=True, default=lambda: new_id("evt"))
    event: Mapped[str] = mapped_column(String)
    task_id: Mapped[str | None] = mapped_column(String, index=True)
    user_id: Mapped[str | None] = mapped_column(String)
    api_client_id: Mapped[str | None] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # none (audit only), pending, delivered, failed
    delivery_status: Mapped[str] = mapped_column(String, default="none")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("api_client_id", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_client_id: Mapped[str] = mapped_column(String)
    key: Mapped[str] = mapped_column(String)
    method: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    api_client_id: Mapped[str | None] = mapped_column(String)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)
