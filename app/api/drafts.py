"""Draft approval and rejection (A2)."""

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import idempotency, webhooks
from ..core.auth import Caller, require_client, scoped_task
from ..core.errors import ApiError
from ..db import get_db
from ..engine import safety
from ..engine.check import TERMINAL, FollowupEngine
from ..models import Followup, Task, User
from ..schemas import ApproveIn, RejectIn, draft_out, followup_out
from .deps import engine, idem_key

router = APIRouter(prefix="/v1/drafts", tags=["drafts"])


@router.get("")
def list_drafts(caller: Caller = Depends(require_client), db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(Followup, Task, User.email)
        .join(Task, Task.id == Followup.task_id)
        .join(User, User.id == Task.owner_user_id)
        .where(
            Followup.status == "drafted",
            Task.api_client_id == caller.client.id,
            Task.owner_user_id.in_(caller.user_ids),
            Task.status == "draft_pending",
        )
        .order_by(Followup.created_at)
    ).all()
    return {"data": [draft_out(f, t, email) for f, t, email in rows]}


def _scoped_followup(db: Session, caller: Caller, draft_id: str) -> tuple[Followup, Task]:
    f = db.scalar(select(Followup).where(Followup.id == draft_id).with_for_update())
    if f is None:
        raise ApiError(404, "not_found", "Draft not found")
    task = scoped_task(db, caller, f.task_id, lock=True)
    return f, task


@router.post("/{draft_id}/approve", status_code=202)
def approve(
    draft_id: str,
    request: Request,
    background: BackgroundTasks,
    body: ApproveIn | None = None,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    eng: FollowupEngine = Depends(engine),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    body = body or ApproveIn()
    queued = []

    def do():
        f, task = _scoped_followup(db, caller, draft_id)
        if task.status in TERMINAL:
            raise ApiError(409, "task_closed", f"Task is {task.status}; the draft was canceled")
        if f.status != "drafted":
            raise ApiError(409, "not_drafted", f"Draft is {f.status}")
        if task.paused:
            raise ApiError(409, "paused", "Task is paused; resume it before approving")
        subject = body.subject if body.subject is not None else f.subject
        text = body.body.replace("\r\n", "\n") if body.body is not None else f.body
        failures = safety.check(
            subject=subject,
            body=text,
            recipient=f.recipient,
            client_email=task.client_email,
            thread_subject=f.thread_subject,
            allowed_facts=f.allowed_facts or [],
            fact_corpus=task.notes or "",
        )
        if failures:
            raise ApiError(422, "safety_check_failed", "; ".join(failures))
        owner = db.get(User, task.owner_user_id)
        now = eng.now()
        f.edited = (subject, text) != (f.subject, f.body) or f.edited
        f.subject, f.body, f.status, f.approved_at = subject, text, "approved", now
        f.scheduled_send_at = eng.send_time(owner.email, task.client_email, owner.timezone, now)
        webhooks.audit(
            db,
            "draft.approved",
            task,
            f"Follow-up {f.sequence_number}" + (" (edited)" if f.edited else ""),
            by=caller.client.name,
        )
        queued.append(f.id)
        return 202, followup_out(f)

    resp = idempotency.run(
        db,
        caller.client.id,
        key,
        "POST",
        request.url.path,
        body.model_dump(mode="json", exclude_none=True),
        do,
    )
    if queued:
        db.commit()
        background.add_task(eng.send_followup, queued[0])
    return resp


@router.post("/{draft_id}/reject")
def reject(
    draft_id: str,
    request: Request,
    body: RejectIn | None = None,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    eng: FollowupEngine = Depends(engine),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    body = body or RejectIn()

    def do():
        f, task = _scoped_followup(db, caller, draft_id)
        if f.status != "drafted":
            raise ApiError(409, "not_drafted", f"Draft is {f.status}")
        f.status, f.rejected_at, f.feedback = "rejected", eng.now(), body.feedback
        # Not a send: followup_count is unchanged. Pause until the owner resumes or updates the task.
        if task.status not in TERMINAL:
            task.status = "followed_up" if task.followup_count else "waiting"
            task.paused, task.pause_reason = True, "draft_rejected"
        webhooks.task_event(
            db,
            "draft.rejected",
            task,
            {
                "draft_id": f.id,
                "sequence_number": f.sequence_number,
                "feedback": body.feedback,
            },
        )
        return 200, followup_out(f)

    return idempotency.run(
        db,
        caller.client.id,
        key,
        "POST",
        request.url.path,
        body.model_dump(mode="json", exclude_none=True),
        do,
    )
