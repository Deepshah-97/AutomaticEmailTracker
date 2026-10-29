"""The follow-up engine: per-task check, drafting, approval-gated sending and crash recovery.

Rules that hold everywhere in this module:
* Work is claimed in a short transaction (row lock + lease); no transaction is held during Gmail
  or Claude calls.
* Gmail is re-read immediately before every send; terminal conditions outrank an approval.
* One followups row per (task, sequence) for its whole life; a send is attempted at most once per
  outbound Message-ID, and an unconfirmed send becomes delivery_unknown, never a blind resend.
* When anything is unclear the engine fails closed: no email, and the reason is recorded.
"""

import logging
import random
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai.context import SCENARIO_GOALS, DraftContext
from ..ai.draft import Drafter, DraftingUnavailable, DraftResult
from ..config import Settings
from ..core import webhooks
from ..core.crypto import TokenCipher
from ..gmail.client import (
    GmailApi,
    GmailAuthError,
    GmailSendRejected,
    GmailSendUnknown,
    GmailUnavailable,
)
from ..gmail.mime import make_message_id, reply_subject
from ..gmail.send import gmail_link, send_followup
from ..gmail.thread import Message, ThreadState, analyze, parse_thread
from ..models import AppSetting, Event, Followup, Task, User
from . import safety
from .business_days import (
    first_check_at,
    in_send_window,
    local_day_bounds,
    next_send_slot,
)

log = logging.getLogger("agent.engine")

ACTIVE = ("waiting", "followed_up")
TERMINAL = ("replied", "stopped", "exhausted", "error")
OPEN_FOLLOWUP = ("drafted", "approved")


@dataclass
class Snapshot:
    task_id: str
    scenario: str
    goal: str | None
    notes: str | None
    client_name: str
    client_email: str
    client_company: str | None
    thread_id: str | None
    allow_new_thread: bool
    followup_count: int
    max_followups: int
    wait_business_days: int
    scenario_trigger_at: datetime | None
    last_outbound_at: datetime | None
    send_mode: str
    owner_id: str
    owner_email: str
    owner_name: str
    owner_tz: str
    signature: str
    style_examples: list[str]
    refresh_token: str | None
    earlier: list[tuple[int, str]]


class FollowupEngine:
    def __init__(
        self,
        settings: Settings,
        session: Callable[[], AbstractContextManager[Session]],
        gmail_factory: Callable[[str], GmailApi],
        drafter: Drafter,
        now: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.session = session
        self._gmail_factory = gmail_factory  # refresh token (plaintext) -> client
        self.drafter = drafter
        self.now = now or (lambda: datetime.now(UTC))
        self.rng = rng or random.Random()
        self.cipher = TokenCipher(settings.token_encryption_key) if settings.token_encryption_key else None

    # ------------------------------------------------------------------ helpers
    def gmail_for(self, encrypted_token: str | None) -> GmailApi:
        if not encrypted_token or self.cipher is None:
            raise GmailAuthError("No Gmail connection")
        return self._gmail_factory(self.cipher.decrypt(encrypted_token))

    def window(self) -> tuple[str, str]:
        return self.settings.send_window_start, self.settings.send_window_end

    @staticmethod
    def globally_paused(db: Session) -> bool:
        row = db.get(AppSetting, "pause_all")
        return bool(row and row.value == "true")

    def _ineligible(self, db: Session, task: Task) -> str | None:
        if task.status not in ACTIVE:
            return f"status_{task.status}"
        if task.paused:
            return "paused"
        if task.followup_count >= task.max_followups:
            return "max_reached"
        owner = db.get(User, task.owner_user_id)
        if owner is None or not owner.active:
            return "owner_inactive"
        if owner.paused:
            return "owner_paused"
        if owner.gmail_status != "connected":
            return "gmail_disconnected"
        if self.globally_paused(db):
            return "paused_globally"
        return None

    def _snapshot(self, db: Session, task: Task) -> Snapshot:
        owner = db.get(User, task.owner_user_id)
        assert owner is not None
        earlier = [
            (f.sequence_number, f.body)
            for f in db.scalars(
                select(Followup)
                .where(Followup.task_id == task.id, Followup.status == "sent")
                .order_by(Followup.sequence_number)
            )
        ]
        return Snapshot(
            task_id=task.id,
            scenario=task.scenario,
            goal=task.goal,
            notes=task.notes,
            client_name=task.client_name,
            client_email=task.client_email,
            client_company=task.client_company,
            thread_id=task.gmail_thread_id,
            allow_new_thread=task.allow_new_thread,
            followup_count=task.followup_count,
            max_followups=task.max_followups,
            wait_business_days=task.wait_business_days,
            scenario_trigger_at=task.scenario_trigger_at,
            last_outbound_at=task.last_outbound_at,
            send_mode=task.send_mode or owner.send_mode,
            owner_id=owner.id,
            owner_email=owner.email,
            owner_name=owner.name,
            owner_tz=owner.timezone,
            signature=owner.signature,
            style_examples=list(owner.style_examples or []),
            refresh_token=owner.google_refresh_token,
            earlier=earlier,
        )

    def _release(self, task_id: str, worker: str) -> None:
        with self.session() as db:
            task = db.get(Task, task_id, with_for_update=True)
            if task and task.claimed_by == worker:
                task.claimed_until, task.claimed_by = None, None

    # ------------------------------------------------------------------ state transitions
    def mark_replied(self, task_id: str, reason: str, at: datetime | None = None) -> None:
        with self.session() as db:
            task = db.get(Task, task_id, with_for_update=True)
            if task is None or task.status in ("replied", "stopped"):
                return
            self._cancel_open(db, task)
            task.status, task.next_check_at, task.last_inbound_at = (
                "replied",
                None,
                at or self.now(),
            )
            task.stop_reason = reason
            webhooks.task_event(db, "task.replied", task, {"reason": reason})

    def stop(self, db: Session, task: Task, reason: str) -> None:
        """Terminal stop inside the caller's transaction (opt-out, meeting booked, closed, manual)."""
        if task.status in TERMINAL:
            return
        self._cancel_open(db, task)
        task.status, task.stop_reason, task.next_check_at = "stopped", reason, None
        webhooks.task_event(db, "task.stopped", task, {"reason": reason})

    def stop_by_id(self, task_id: str, reason: str) -> None:
        with self.session() as db:
            task = db.get(Task, task_id, with_for_update=True)
            if task:
                self.stop(db, task, reason)

    def _cancel_open(self, db: Session, task: Task) -> None:
        for f in db.scalars(
            select(Followup).where(Followup.task_id == task.id, Followup.status.in_(OPEN_FOLLOWUP)).with_for_update()
        ):
            f.status, f.canceled_at = "canceled", self.now()

    def needs_review(self, task_id: str, code: str, message: str) -> None:
        """Pause and surface the problem; never send while a human decision is needed."""
        with self.session() as db:
            task = db.get(Task, task_id, with_for_update=True)
            if task is None or task.status in TERMINAL:
                return
            task.paused, task.pause_reason, task.review_required = True, code, True
            task.error_message = message
            webhooks.task_event(db, "task.error", task, {"code": code, "message": message})

    def disconnect_user(self, user_id: str, reason: str) -> None:
        """invalid_grant: delete tokens, pause the user's tasks, fire user.disconnected."""
        with self.session() as db:
            user = db.get(User, user_id, with_for_update=True)
            if user is None:
                return
            already = user.gmail_status == "disconnected"
            user.gmail_status, user.google_refresh_token = "disconnected", None
            for task in db.scalars(
                select(Task)
                .where(
                    Task.owner_user_id == user_id,
                    Task.status.in_(("waiting", "followed_up", "draft_pending")),
                    Task.paused.is_(False),
                )
                .with_for_update()
            ):
                task.paused, task.pause_reason = True, "gmail_disconnected"
            if not already:
                webhooks.user_event(
                    db,
                    "user.disconnected",
                    user_id,
                    {"user_id": user_id, "email": user.email, "reason": reason},
                )

    # ------------------------------------------------------------------ the per-task check
    def check_task(self, task_id: str, *, force: bool = False) -> str:
        worker = uuid.uuid4().hex
        now = self.now()
        with self.session() as db:
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update(skip_locked=True))
            if task is None:
                return "locked"
            if task.claimed_until and task.claimed_until > now:
                return "claimed"
            reason = self._ineligible(db, task)
            if reason:
                return reason
            if not force and task.next_check_at and task.next_check_at > now:
                return "not_due"
            task.claimed_until = now + timedelta(seconds=self.settings.claim_lease_seconds)
            task.claimed_by = worker
            snap = self._snapshot(db, task)
        try:
            return self._check_claimed(snap, force=force)
        except GmailAuthError as exc:
            self.disconnect_user(snap.owner_id, str(exc))
            return "gmail_disconnected"
        except (GmailUnavailable, DraftingUnavailable) as exc:
            log.warning("Check of %s postponed: %s", task_id, exc)
            with self.session() as db:
                task = db.get(Task, task_id, with_for_update=True)
                if task:
                    task.next_check_at = self.now() + timedelta(hours=1)
                    webhooks.audit(db, "check.postponed", task, exc.__class__.__name__)
            return "postponed"
        finally:
            self._release(task_id, worker)

    def _load_thread(self, snap: Snapshot, gmail: GmailApi) -> tuple[str | None, list[Message]] | str:
        if snap.thread_id:
            return snap.thread_id, parse_thread(gmail.get_thread(snap.thread_id))
        candidates = gmail.search_threads(f"from:{snap.client_email} OR to:{snap.client_email}", 5)
        if len(candidates) == 1:
            thread_id = candidates[0]["id"]
            with self.session() as db:
                task = db.get(Task, snap.task_id, with_for_update=True)
                if task and not task.gmail_thread_id:
                    task.gmail_thread_id = thread_id
            return thread_id, parse_thread(gmail.get_thread(thread_id))
        if len(candidates) > 1:
            self.needs_review(
                snap.task_id,
                "thread_selection_required",
                f"{len(candidates)} threads with {snap.client_email} found; pick one.",
            )
            return "needs_thread"
        if snap.scenario == "new_lead" and snap.allow_new_thread:
            return None, []
        self.needs_review(
            snap.task_id,
            "no_thread_found",
            f"No Gmail thread with {snap.client_email} found.",
        )
        return "needs_thread"

    def _baseline(self, snap: Snapshot, state: ThreadState) -> datetime | None:
        if snap.followup_count > 0 and snap.last_outbound_at:
            return snap.last_outbound_at
        if snap.scenario == "no_reply":
            return state.last_owner_outbound.date if state.last_owner_outbound else None
        if snap.scenario == "new_lead":
            if snap.scenario_trigger_at:
                return snap.scenario_trigger_at
            return state.messages[0].date if state.messages else None
        return snap.scenario_trigger_at

    def _evaluate(self, snap: Snapshot, messages: list[Message]) -> ThreadState | str:
        since = snap.scenario_trigger_at if snap.scenario == "new_lead" else None
        if snap.followup_count > 0 and snap.last_outbound_at:
            since = max(filter(None, [since, snap.last_outbound_at]))
        state = analyze(messages, snap.owner_email, snap.client_email, since=since)
        if state.opt_out:
            self.stop_by_id(snap.task_id, "opted_out")
            return "stopped"
        if state.client_reply:
            self.mark_replied(snap.task_id, "client_replied", state.client_reply.date)
            return "replied"
        if state.ambiguous and not (snap.scenario == "new_lead" and not state.last_owner_outbound):
            self.needs_review(snap.task_id, "review_required", state.ambiguous)
            return "needs_review"
        return state

    def build_context(self, snap: Snapshot, state: ThreadState) -> DraftContext:
        return DraftContext(
            scenario=snap.scenario,
            goal=snap.goal or SCENARIO_GOALS.get(snap.scenario, ""),
            sequence_number=snap.followup_count + 1,
            max_followups=snap.max_followups,
            owner_name=snap.owner_name,
            owner_email=snap.owner_email,
            owner_timezone=snap.owner_tz,
            signature=snap.signature,
            style_examples=snap.style_examples,
            client_name=snap.client_name,
            client_company=snap.client_company,
            client_email=snap.client_email,
            notes=snap.notes or "",
            thread=state.messages,
            earlier_followups=snap.earlier,
            thread_subject=state.subject,
            today=self.now(),
            token_budget=self.settings.thread_token_budget,
        )

    def generate(self, ctx: DraftContext, *, replying: bool) -> tuple[DraftResult, list[str], float, int, int]:
        """Draft, check, regenerate once on failure; returns (result, remaining failures, cost, in, out)."""
        cost = tok_in = tok_out = 0
        failures: list[str] = []
        result: DraftResult | None = None
        for attempt in range(2):
            result = self.drafter.draft(ctx, feedback=failures if attempt else None)
            cost += result.cost_usd
            tok_in += result.input_tokens
            tok_out += result.output_tokens
            if replying:
                result.subject = reply_subject(ctx.thread_subject)
            failures = safety.check(
                subject=result.subject,
                body=result.body,
                recipient=ctx.client_email,
                client_email=ctx.client_email,
                thread_subject=ctx.thread_subject if replying else None,
                allowed_facts=[],
                fact_corpus=ctx.fact_corpus(),
            )
            if not failures:
                break
        assert result is not None
        return result, failures, cost, tok_in, tok_out

    def _check_claimed(self, snap: Snapshot, *, force: bool) -> str:
        gmail = self.gmail_for(snap.refresh_token)
        loaded = self._load_thread(snap, gmail)
        if isinstance(loaded, str):
            return loaded
        thread_id, messages = loaded
        evaluated = self._evaluate(snap, messages)
        if isinstance(evaluated, str):
            return evaluated
        state = evaluated
        now = self.now()
        baseline = self._baseline(snap, state)
        if baseline is None:
            self.needs_review(
                snap.task_id,
                "missing_baseline",
                "No valid scheduling baseline (no owner message in the thread and no trigger time).",
            )
            return "needs_review"
        due = first_check_at(baseline, snap.wait_business_days, snap.owner_tz, *self.window())
        if not force and due > now:
            with self.session() as db:
                task = db.get(Task, snap.task_id, with_for_update=True)
                if task:
                    task.next_check_at = due
                    if state.last_owner_outbound and not task.last_outbound_at:
                        task.last_outbound_at = state.last_owner_outbound.date
            return "scheduled"

        ctx = self.build_context(snap, state)
        replying = thread_id is not None
        result, failures, cost, tok_in, tok_out = self.generate(ctx, replying=replying)
        needs_review = result.needs_review or bool(failures)
        last = state.last_message
        return self._save_draft(
            snap,
            ctx,
            result,
            failures,
            needs_review,
            cost,
            tok_in,
            tok_out,
            thread_id,
            last.rfc_message_id if last else None,
            last.references if last else None,
            state.subject if replying else None,
        )

    def _save_draft(
        self,
        snap: Snapshot,
        ctx: DraftContext,
        result: DraftResult,
        failures: list[str],
        needs_review: bool,
        cost: float,
        tok_in: int,
        tok_out: int,
        thread_id: str | None,
        in_reply_to: str | None,
        references: str | None,
        thread_subject: str | None,
    ) -> str:
        now = self.now()
        auto_followup_id = None
        with self.session() as db:
            task = db.get(Task, snap.task_id, with_for_update=True)
            if task is None or task.status not in ACTIVE or task.paused or task.followup_count != snap.followup_count:
                return "changed"
            seq = task.followup_count + 1
            f = db.scalar(
                select(Followup).where(Followup.task_id == task.id, Followup.sequence_number == seq).with_for_update()
            )
            if f and f.status not in ("rejected", "failed", "canceled"):
                return f"exists_{f.status}"
            if f:
                # Redraft within the same sequence record; archive the old version first (A2).
                webhooks.audit(
                    db,
                    "draft.revision_archived",
                    task,
                    None,
                    followup_id=f.id,
                    revision=f.draft_revision,
                    status=f.status,
                    subject=f.subject,
                    body=f.body,
                    feedback=f.feedback,
                )
                f.draft_revision += 1
            else:
                f = Followup(
                    task_id=task.id,
                    sequence_number=seq,
                    idempotency_key=f"{task.id}:{seq}",
                )
                db.add(f)
            f.status, f.subject, f.body, f.original_body = (
                "drafted",
                result.subject,
                result.body,
                result.body,
            )
            f.edited, f.feedback, f.error = False, None, None
            f.needs_review, f.reason, f.safety_failures = (
                needs_review,
                result.reason,
                failures,
            )
            f.context_summary = ctx.safe_summary()
            f.allowed_facts = safety.extract_facts(ctx.fact_corpus())
            f.thread_subject = thread_subject
            f.recipient, f.gmail_thread_id = task.client_email, thread_id
            f.in_reply_to, f.references = in_reply_to, references
            f.model, f.input_tokens, f.output_tokens = result.model, tok_in, tok_out
            f.cost_usd = cost
            f.created_at, f.approved_at, f.rejected_at, f.canceled_at, f.failed_at = (
                now,
                None,
                None,
                None,
                None,
            )
            f.outbound_attempt_id = f.outbound_message_id = None
            task.status, task.next_check_at = "draft_pending", None
            db.flush()
            if snap.send_mode == "auto" and not needs_review:
                f.status, f.approved_at = "approved", now
                f.scheduled_send_at = self.send_time(snap.owner_email, task.client_email, snap.owner_tz, now)
                webhooks.audit(db, "draft.auto_approved", task, f"Follow-up {seq}")
                auto_followup_id = f.id
            else:
                webhooks.task_event(
                    db,
                    "draft.ready",
                    task,
                    {
                        "draft_id": f.id,
                        "sequence_number": seq,
                        "subject": f.subject,
                        "needs_review": needs_review,
                    },
                )
        if auto_followup_id:
            self.send_followup(auto_followup_id)
            return "auto_approved"
        return "drafted"

    def send_time(self, owner_email: str, client_email: str, tz: str, now: datetime) -> datetime:
        """Next valid send time. TEST_MODE allowlisted pairs skip the window and random delay only."""
        if self.settings.test_mode_allows(owner_email, client_email):
            return now
        delay = timedelta(minutes=self.rng.uniform(0, self.settings.random_delay_max_minutes))
        return next_send_slot(now + delay, tz, *self.window())

    # ------------------------------------------------------------------ preview (never saved)
    def preview(self, task_id: str) -> dict[str, Any]:
        with self.session() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise LookupError(task_id)
            snap = self._snapshot(db, task)
        gmail = self.gmail_for(snap.refresh_token)
        if snap.thread_id:
            thread_id, messages = (
                snap.thread_id,
                parse_thread(gmail.get_thread(snap.thread_id)),
            )
        else:
            thread_id, messages = None, []
        state = analyze(messages, snap.owner_email, snap.client_email)
        ctx = self.build_context(snap, state)
        result, failures, cost, _, _ = self.generate(ctx, replying=thread_id is not None)
        log.info("Preview for %s cost $%.4f", task_id, cost)
        return {
            "subject": result.subject,
            "body": result.body,
            "needs_review": result.needs_review or bool(failures),
            "reason": result.reason,
            "context_summary": ctx.safe_summary(),
            "safety_failures": failures,
            "sequence_number": ctx.sequence_number,
        }

    # ------------------------------------------------------------------ sending
    def send_followup(self, followup_id: str) -> str:
        now = self.now()
        with self.session() as db:
            f = db.scalar(select(Followup).where(Followup.id == followup_id).with_for_update(skip_locked=True))
            if f is None:
                return "locked"
            if f.status != "approved":
                return f.status
            if f.scheduled_send_at and f.scheduled_send_at > now:
                return "scheduled"
            task = db.get(Task, f.task_id, with_for_update=True)
            owner = db.get(User, task.owner_user_id) if task else None
            if task is None or owner is None:
                return "missing"
            if task.status in TERMINAL:
                f.status, f.canceled_at = "canceled", now
                return "canceled"
            if task.paused or owner.paused or owner.gmail_status != "connected" or self.globally_paused(db):
                return "held"
            held = self._send_gate(db, f, task, owner, now)
            if held:
                return held
            # Compare-and-swap approved -> sending under the row lock, with a fresh outbound identity.
            f.status, f.sending_at = "sending", now
            f.outbound_attempt_id = uuid.uuid4().hex
            f.outbound_message_id = make_message_id(owner.email)
            f.claimed_until = now + timedelta(seconds=self.settings.claim_lease_seconds)
            send = dict(
                owner_id=owner.id,
                owner_email=owner.email,
                owner_name=owner.name,
                token=owner.google_refresh_token,
                to=f.recipient,
                subject=f.subject,
                body=f.body,
                message_id=f.outbound_message_id,
                thread_id=f.gmail_thread_id,
                in_reply_to=f.in_reply_to,
                references=f.references,
                client_email=task.client_email,
                task_id=task.id,
                last_outbound_at=task.last_outbound_at,
            )
        return self._send_claimed(followup_id, send)

    def _send_gate(self, db: Session, f: Followup, task: Task, owner: User, now: datetime) -> str | None:
        """Sending rules checked inside the claim transaction. Returns a reason to hold, or None."""
        s = self.settings
        allow_test = s.test_mode_allows(owner.email, task.client_email)
        if s.test_mode and not allow_test:
            f.status, f.failed_at, f.error = (
                "failed",
                now,
                "TEST_MODE: recipient or sender not on the allowlist",
            )
            task.paused, task.pause_reason, task.review_required = (
                True,
                "not_allowlisted",
                True,
            )
            webhooks.task_event(db, "task.error", task, {"code": "not_allowlisted", "message": f.error})
            return "blocked_test_mode"
        if not s.test_mode and not s.sending_enabled:
            if f.error != "sending_disabled":
                f.error = "sending_disabled"
                webhooks.audit(
                    db,
                    "send.held",
                    task,
                    "Sending is disabled by the operator (SENDING_ENABLED=false)",
                )
            return "sending_disabled"
        if f.recipient.lower() != task.client_email.lower():
            f.status, f.failed_at, f.error = (
                "failed",
                now,
                "recipient changed since drafting",
            )
            webhooks.task_event(
                db,
                "task.error",
                task,
                {"code": "recipient_mismatch", "message": f.error},
            )
            return "recipient_mismatch"
        if not allow_test:
            if not in_send_window(now, owner.timezone, *self.window()):
                f.scheduled_send_at = next_send_slot(now, owner.timezone, *self.window())
                return "rescheduled_window"
            start, end = local_day_bounds(now, owner.timezone)
            to_client = db.scalar(
                select(func.count())
                .select_from(Followup)
                .where(
                    func.lower(Followup.recipient) == task.client_email.lower(),
                    Followup.status == "sent",
                    Followup.sent_at >= start,
                    Followup.sent_at < end,
                )
            )
            by_user = db.scalar(
                select(func.count())
                .select_from(Followup)
                .join(Task, Task.id == Followup.task_id)
                .where(
                    Task.owner_user_id == owner.id,
                    Followup.status == "sent",
                    Followup.sent_at >= start,
                    Followup.sent_at < end,
                )
            )
            if to_client >= 1 or by_user >= s.max_sends_per_user_per_day:
                f.scheduled_send_at = next_send_slot(end, owner.timezone, *self.window())
                return "rescheduled_limit"
        return None

    def _revert_to_approved(self, followup_id: str, reason: str, retry_in: timedelta) -> None:
        with self.session() as db:
            f = db.get(Followup, followup_id, with_for_update=True)
            if f and f.status == "sending":
                f.status, f.error = "approved", reason
                f.outbound_attempt_id = f.outbound_message_id = None
                f.scheduled_send_at, f.claimed_until = self.now() + retry_in, None

    def _send_claimed(self, followup_id: str, s: dict[str, Any]) -> str:
        try:
            gmail = self.gmail_for(s["token"])
            # Pre-send recheck (fail closed if Gmail cannot be read).
            if s["thread_id"]:
                messages = parse_thread(gmail.get_thread(s["thread_id"]))
                state = analyze(
                    messages,
                    s["owner_email"],
                    s["client_email"],
                    since=s["last_outbound_at"],
                )
                if state.opt_out or state.client_reply:
                    with self.session() as db:
                        f = db.get(Followup, followup_id, with_for_update=True)
                        task = db.get(Task, s["task_id"], with_for_update=True)
                        f.status, f.canceled_at, f.claimed_until = (
                            "canceled",
                            self.now(),
                            None,
                        )
                        if state.opt_out:
                            self.stop(db, task, "opted_out")
                            return "stopped"
                        self._cancel_open(db, task)
                        task.status, task.next_check_at = "replied", None
                        task.last_inbound_at, task.stop_reason = (
                            state.client_reply.date,
                            "client_replied",
                        )
                        webhooks.task_event(db, "task.replied", task, {"reason": "client_replied"})
                    return "replied"
                if state.ambiguous:
                    # Unsent: back to an approvable draft, and pause until a human has looked.
                    with self.session() as db:
                        f = db.get(Followup, followup_id, with_for_update=True)
                        f.status, f.claimed_until = "drafted", None
                        f.outbound_attempt_id = f.outbound_message_id = None
                    self.needs_review(s["task_id"], "review_required", state.ambiguous)
                    return "needs_review"
        except GmailAuthError as exc:
            self._revert_to_approved(followup_id, "gmail_disconnected", timedelta(hours=1))
            self.disconnect_user(s["owner_id"], str(exc))
            return "gmail_disconnected"
        except GmailUnavailable as exc:
            self._revert_to_approved(followup_id, f"pre-send check failed: {exc}", timedelta(minutes=15))
            return "postponed"

        try:
            resp = send_followup(
                gmail,
                sender_email=s["owner_email"],
                sender_name=s["owner_name"],
                to=s["to"],
                subject=s["subject"],
                body=s["body"],
                message_id=s["message_id"],
                thread_id=s["thread_id"],
                in_reply_to=s["in_reply_to"],
                references=s["references"],
            )
        except GmailSendRejected as exc:
            with self.session() as db:
                f = db.get(Followup, followup_id, with_for_update=True)
                task = db.get(Task, s["task_id"], with_for_update=True)
                f.status, f.failed_at, f.error, f.claimed_until = (
                    "failed",
                    self.now(),
                    str(exc),
                    None,
                )
                task.paused, task.pause_reason, task.review_required = (
                    True,
                    "send_failed",
                    True,
                )
                webhooks.task_event(db, "task.error", task, {"code": "send_failed", "message": str(exc)})
            return "failed"
        except (GmailSendUnknown, GmailAuthError, GmailUnavailable) as exc:
            log.warning(
                "Send of %s unconfirmed: %s; reconciling by Message-ID",
                followup_id,
                exc,
            )
            return self.reconcile(followup_id)
        return self.finalize_sent(followup_id, resp.get("id"), resp.get("threadId"))

    def finalize_sent(self, followup_id: str, gmail_message_id: str | None, thread_id: str | None) -> str:
        now = self.now()
        with self.session() as db:
            f = db.get(Followup, followup_id, with_for_update=True)
            if f is None or f.status == "sent":
                return "sent"
            task = db.get(Task, f.task_id, with_for_update=True)
            owner = db.get(User, task.owner_user_id, with_for_update=True)
            f.status, f.sent_at, f.gmail_message_id, f.claimed_until, f.error = (
                "sent",
                now,
                gmail_message_id,
                None,
                None,
            )
            if thread_id and not f.gmail_thread_id:
                f.gmail_thread_id = thread_id
            if thread_id and not task.gmail_thread_id:
                task.gmail_thread_id = thread_id
            task.followup_count += 1  # exactly once: guarded by the status check above under the row lock
            task.last_outbound_at = now
            final = task.followup_count >= task.max_followups
            if task.status not in ("replied", "stopped"):
                task.status = "exhausted" if final else "followed_up"
                task.next_check_at = (
                    None if final else first_check_at(now, task.wait_business_days, owner.timezone, *self.window())
                )
            if f.edited and owner:
                owner.style_examples = ([*(owner.style_examples or []), f.body])[-10:]
            webhooks.task_event(
                db,
                "followup.sent",
                task,
                {
                    "draft_id": f.id,
                    "sequence_number": f.sequence_number,
                    "subject": f.subject,
                    "body": f.body,
                    "sent_at": now.isoformat().replace("+00:00", "Z"),
                    "gmail_link": gmail_link(f.gmail_thread_id),
                },
            )
            if final and task.status == "exhausted":
                webhooks.task_event(db, "task.exhausted", task, {"sequence_number": f.sequence_number})
        return "sent"

    def reconcile(self, followup_id: str) -> str:
        """After a crash or ambiguous send: look for our Message-ID; never resend blindly."""
        with self.session() as db:
            f = db.get(Followup, followup_id)
            if f is None or f.status not in ("sending", "delivery_unknown"):
                return f.status if f else "missing"
            task = db.get(Task, f.task_id)
            owner = db.get(User, task.owner_user_id)
            token, message_id = owner.google_refresh_token, f.outbound_message_id
        found = None
        try:
            if message_id:
                found = self.gmail_for(token).find_by_message_id(message_id)
        except (GmailAuthError, GmailUnavailable, GmailSendUnknown) as exc:
            log.warning("Reconcile lookup failed for %s: %s", followup_id, exc)
        if found:
            return self.finalize_sent(followup_id, found.get("id"), found.get("threadId"))
        with self.session() as db:
            f = db.get(Followup, followup_id, with_for_update=True)
            if f.status == "delivery_unknown":
                return "delivery_unknown"
            task = db.get(Task, f.task_id, with_for_update=True)
            f.status, f.claimed_until = "delivery_unknown", None
            f.error = "Gmail did not confirm the send and the message was not found; not resending"
            task.paused, task.pause_reason, task.review_required = (
                True,
                "delivery_unknown",
                True,
            )
            webhooks.task_event(db, "task.error", task, {"code": "delivery_unknown", "message": f.error})
            log.error("ALERT delivery_unknown for followup %s (task %s)", followup_id, task.id)
        return "delivery_unknown"

    def recover_stale_sends(self) -> int:
        """Crash recovery: followups stuck in 'sending' past their lease are reconciled by Message-ID."""
        with self.session() as db:
            ids = list(
                db.scalars(
                    select(Followup.id).where(
                        Followup.status == "sending",
                        Followup.claimed_until < self.now(),
                    )
                )
            )
        for fid in ids:
            self.reconcile(fid)
        return len(ids)

    # ------------------------------------------------------------------ batch jobs
    def due_task_ids(self, limit: int = 200) -> list[str]:
        with self.session() as db:
            return list(
                db.scalars(
                    select(Task.id)
                    .where(
                        Task.status.in_(ACTIVE),
                        Task.paused.is_(False),
                        Task.next_check_at <= self.now(),
                        Task.followup_count < Task.max_followups,
                    )
                    .order_by(Task.next_check_at)
                    .limit(limit)
                )
            )

    def run_due_checks(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for tid in self.due_task_ids():
            try:
                outcome = self.check_task(tid)
            except Exception:  # one bad task must not stop the batch
                log.exception("Check failed for %s", tid)
                self._mark_error(tid)
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts

    def _mark_error(self, task_id: str) -> None:
        with self.session() as db:
            task = db.get(Task, task_id, with_for_update=True)
            if task and task.status not in TERMINAL:
                task.next_check_at = self.now() + timedelta(hours=1)
                webhooks.audit(db, "check.failed", task, "Unhandled error; retrying in an hour")

    def send_due(self) -> dict[str, int]:
        with self.session() as db:
            ids = list(
                db.scalars(
                    select(Followup.id).where(
                        Followup.status == "approved",
                        Followup.scheduled_send_at <= self.now(),
                    )
                )
            )
        counts: dict[str, int] = {}
        for fid in ids:
            try:
                outcome = self.send_followup(fid)
            except Exception:
                log.exception("Send failed for %s", fid)
                outcome = "error"
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts

    def daily_summary(self) -> None:
        from ..models import ApiClient

        since = self.now() - timedelta(days=1)
        with self.session() as db:
            for client in db.scalars(select(ApiClient).where(ApiClient.active.is_(True))):
                task_ids = select(Task.id).where(Task.api_client_id == client.id)
                fq = select(func.count()).select_from(Followup).where(Followup.task_id.in_(task_ids))
                sent = db.scalar(fq.where(Followup.status == "sent", Followup.sent_at >= since))
                drafted = db.scalar(fq.where(Followup.created_at >= since))
                spend = db.scalar(
                    select(func.coalesce(func.sum(Followup.cost_usd), 0.0)).where(
                        Followup.task_id.in_(task_ids), Followup.created_at >= since
                    )
                )
                eq = (
                    select(func.count())
                    .select_from(Event)
                    .where(Event.api_client_id == client.id, Event.created_at >= since)
                )
                replied = db.scalar(eq.where(Event.event == "task.replied"))
                errors = db.scalar(eq.where(Event.event == "task.error"))
                webhooks.client_event(
                    db,
                    "daily.summary",
                    client,
                    {
                        "sent": sent,
                        "drafted": drafted,
                        "replied": replied,
                        "errors": errors,
                        "ai_spend_usd": round(float(spend or 0), 4),
                    },
                )

    def check_alerts(self) -> list[str]:
        alerts = []
        now = self.now()
        with self.session() as db:
            failed = db.scalar(
                select(func.count())
                .select_from(Followup)
                .where(
                    Followup.status.in_(("failed", "delivery_unknown")),
                    Followup.updated_at >= now - timedelta(hours=1),
                )
            )
            spend = db.scalar(
                select(func.coalesce(func.sum(Followup.cost_usd), 0.0)).where(
                    Followup.created_at >= now - timedelta(days=1)
                )
            )
        if failed > self.settings.send_failure_alert_per_hour:
            alerts.append(f"{failed} sends failed in the last hour")
        if float(spend or 0) > self.settings.ai_daily_alert_usd:
            alerts.append(f"AI spend ${float(spend):.2f} in the last 24h")
        for a in alerts:
            log.error("ALERT %s", a)
            try:
                import sentry_sdk

                sentry_sdk.capture_message(a, level="error")
            except Exception:  # pragma: no cover
                pass
        return alerts

    def purge_old(self) -> int:
        cutoff = self.now() - timedelta(days=self.settings.followup_retention_days)
        with self.session() as db:
            old = list(db.scalars(select(Followup).where(Followup.status == "sent", Followup.sent_at < cutoff)))
            for f in old:
                f.subject, f.body, f.original_body = "", "", None
            return len(old)
