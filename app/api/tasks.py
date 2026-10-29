"""Tasks API (spec section 5 + v1.1 amendments)."""

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai.draft import DraftingUnavailable
from ..core import idempotency, webhooks
from ..core.auth import Caller, require_client, scoped_task
from ..core.errors import ApiError
from ..db import get_db, session_scope
from ..engine.business_days import first_check_at
from ..engine.check import ACTIVE, TERMINAL, FollowupEngine
from ..gmail.client import GmailAuthError, GmailUnavailable
from ..models import Event, Followup, Task, User
from ..schemas import (
    REQUIRES_TRIGGER,
    SCENARIO_DEFAULTS,
    TRIGGER_TYPES,
    ExternalEventIn,
    StopIn,
    TaskCreate,
    TaskUpdate,
    task_detail_out,
    task_out,
)
from .deps import engine, idem_key

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


def detail(db: Session, task: Task) -> dict[str, Any]:
    owner = db.get(User, task.owner_user_id)
    followups = list(db.scalars(select(Followup).where(Followup.task_id == task.id).order_by(Followup.sequence_number)))
    events = list(db.scalars(select(Event).where(Event.task_id == task.id).order_by(Event.id)))
    return task_detail_out(task, owner.email if owner else "", followups, events)


def _initial_next_check(body: TaskCreate, owner: User, wait: int, eng: FollowupEngine):
    """Initial schedule from the scenario baseline (A4). no_reply reads the thread first to find it."""
    now = eng.now()
    if body.scenario_trigger_at:
        return first_check_at(body.scenario_trigger_at, wait, owner.timezone, *eng.window())
    return now


@router.post("", status_code=201)
def create_task(
    body: TaskCreate,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    eng: FollowupEngine = Depends(engine),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    def do() -> tuple[int, Any]:
        owner = db.scalar(select(User).where(User.email == str(body.owner_email).lower()))
        if owner is None or not owner.active or not caller.can_act_for(owner.id):
            raise ApiError(
                403,
                "owner_not_authorized",
                "Owner is not a provisioned user for this API client",
            )
        defaults = SCENARIO_DEFAULTS[body.scenario]
        if body.scenario == "custom":
            if not body.goal:
                raise ApiError(422, "missing_goal", "custom scenario requires a goal")
            if body.wait_business_days is None or body.max_followups is None:
                raise ApiError(
                    422,
                    "missing_wait_rule",
                    "custom scenario requires wait_business_days and max_followups",
                )
        if body.scenario in REQUIRES_TRIGGER and not body.scenario_trigger_at:
            raise ApiError(
                422,
                "missing_baseline",
                f"{body.scenario} requires scenario_trigger_at (no date is inferred)",
            )
        if body.scenario == "new_lead" and not (body.gmail_thread_id or body.scenario_trigger_at):
            raise ApiError(
                422,
                "missing_baseline",
                "new_lead requires a gmail_thread_id or the lead's received time",
            )
        if body.trigger_type and body.trigger_type not in TRIGGER_TYPES.get(body.scenario, set()):
            raise ApiError(
                422,
                "invalid_trigger_type",
                f"{body.trigger_type} is not valid for {body.scenario}",
            )
        if body.allow_new_thread and body.scenario != "new_lead":
            raise ApiError(422, "new_thread_not_allowed", "Only new_lead may start a new thread")
        if db.scalar(
            select(Task.id).where(
                Task.api_client_id == caller.client.id,
                Task.external_id == body.external_id,
            )
        ):
            raise ApiError(
                409,
                "duplicate_external_id",
                "A task with this external_id already exists",
            )
        wait = body.wait_business_days if body.wait_business_days is not None else defaults[0]  # type: ignore[index]
        max_f = body.max_followups if body.max_followups is not None else defaults[1]  # type: ignore[index]
        task = Task(
            external_id=body.external_id,
            api_client_id=caller.client.id,
            owner_user_id=owner.id,
            scenario=body.scenario,
            goal=body.goal,
            client_name=body.client.name,
            client_email=str(body.client.email).lower(),
            client_company=body.client.company,
            gmail_thread_id=body.gmail_thread_id,
            allow_new_thread=body.allow_new_thread,
            notes=body.notes,
            status="waiting",
            paused=body.paused,
            max_followups=max_f,
            wait_business_days=wait,
            send_mode=body.send_mode,
            scenario_trigger_at=body.scenario_trigger_at,
            trigger_type=body.trigger_type,
            next_check_at=_initial_next_check(body, owner, wait, eng),
        )
        db.add(task)
        db.flush()
        webhooks.audit(db, "task.created", task, f"Created by {caller.client.name}")
        return 201, detail(db, task)

    return idempotency.run(db, caller.client.id, key, "POST", "/v1/tasks", body.model_dump(mode="json"), do)


@router.get("")
def list_tasks(
    status: str | None = None,
    owner_email: str | None = None,
    scenario: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
) -> dict:
    limit = max(1, min(limit, 200))
    q = (
        select(Task, User.email)
        .join(User, User.id == Task.owner_user_id)
        .where(
            Task.api_client_id == caller.client.id,
            Task.owner_user_id.in_(caller.user_ids),
        )
    )
    if status:
        q = q.where(Task.status == status)
    if owner_email:
        q = q.where(User.email == owner_email.lower())
    if scenario:
        q = q.where(Task.scenario == scenario)
    if cursor:
        q = q.where(Task.id > cursor)
    rows = db.execute(q.order_by(Task.id).limit(limit + 1)).all()
    data = [task_out(t, email) for t, email in rows[:limit]]
    return {
        "data": data,
        "next_cursor": rows[limit - 1][0].id if len(rows) > limit else None,
    }


@router.get("/{task_id}")
def get_task(
    task_id: str,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
) -> dict:
    return detail(db, scoped_task(db, caller, task_id))


def _mutate(request: Request, db: Session, caller: Caller, key: str | None, body: Any, fn) -> JSONResponse:
    return idempotency.run(db, caller.client.id, key, request.method, request.url.path, body, fn)


@router.patch("/{task_id}")
def update_task(
    task_id: str,
    body: TaskUpdate,
    request: Request,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    eng: FollowupEngine = Depends(engine),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    def do():
        task = scoped_task(db, caller, task_id, lock=True)
        changes = body.model_dump(exclude_unset=True)
        if task.status in TERMINAL and set(changes) - {"notes"}:
            raise ApiError(409, "terminal", f"Task is {task.status}")
        for k, v in changes.items():
            setattr(task, k, v)
        if "gmail_thread_id" in changes and task.pause_reason in (
            "thread_selection_required",
            "no_thread_found",
        ):
            task.paused, task.pause_reason, task.review_required, task.error_message = (
                False,
                None,
                False,
                None,
            )
            task.next_check_at = eng.now()
        webhooks.audit(db, "task.updated", task, ", ".join(sorted(changes)))
        return 200, detail(db, task)

    return _mutate(request, db, caller, key, body.model_dump(mode="json", exclude_unset=True), do)


@router.post("/{task_id}/pause")
def pause(
    task_id: str,
    request: Request,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    def do():
        task = scoped_task(db, caller, task_id, lock=True)
        if not task.paused:
            task.paused, task.pause_reason = True, "paused_by_user"
            webhooks.audit(db, "task.paused", task)
        return 200, detail(db, task)

    return _mutate(request, db, caller, key, None, do)


@router.post("/{task_id}/resume")
def resume(
    task_id: str,
    request: Request,
    background: BackgroundTasks,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    eng: FollowupEngine = Depends(engine),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    redraft = []

    def do():
        task = scoped_task(db, caller, task_id, lock=True)
        if task.status in TERMINAL:
            raise ApiError(409, "terminal", f"Task is {task.status}")
        owner = db.get(User, task.owner_user_id)
        if task.pause_reason == "gmail_disconnected" and owner and owner.gmail_status != "connected":
            raise ApiError(409, "gmail_disconnected", "Reconnect the owner's Gmail first")
        if task.paused:
            if task.pause_reason == "draft_rejected":
                task.next_check_at = eng.now()
                redraft.append(task.id)
            task.paused, task.pause_reason, task.review_required, task.error_message = (
                False,
                None,
                False,
                None,
            )
            webhooks.audit(db, "task.resumed", task)
        return 200, detail(db, task)

    resp = _mutate(request, db, caller, key, None, do)
    if redraft:
        db.commit()
        background.add_task(eng.check_task, redraft[0], force=True)
    return resp


@router.post("/{task_id}/stop")
def stop(
    task_id: str,
    request: Request,
    body: StopIn | None = None,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    eng: FollowupEngine = Depends(engine),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    reason = (body.reason if body else None) or "stopped_by_user"

    def do():
        task = scoped_task(db, caller, task_id, lock=True)
        eng.stop(db, task, reason)
        return 200, detail(db, task)

    return _mutate(request, db, caller, key, {"reason": reason}, do)


@router.post("/{task_id}/events")
def external_event(
    task_id: str,
    body: ExternalEventIn,
    request: Request,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    eng: FollowupEngine = Depends(engine),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    def do():
        task = scoped_task(db, caller, task_id, lock=True)
        webhooks.audit(
            db,
            f"external.{body.type}",
            task,
            body.note,
            source_event_id=body.source_event_id,
            occurred_at=body.occurred_at.isoformat(),
        )
        eng.stop(db, task, body.type)
        return 200, detail(db, task)

    # source_event_id makes the event idempotent even without an Idempotency-Key header.
    return _mutate(
        request,
        db,
        caller,
        key or f"event:{task_id}:{body.source_event_id}",
        body.model_dump(mode="json"),
        do,
    )


@router.post("/{task_id}/preview")
def preview(
    task_id: str,
    caller: Caller = Depends(require_client),
    eng: FollowupEngine = Depends(engine),
) -> dict:
    # Short sessions only: no transaction is held during the Gmail and Claude calls.
    with session_scope() as db:
        task = scoped_task(db, caller, task_id)
        if task.status in TERMINAL:
            raise ApiError(409, "terminal", f"Task is {task.status}")
    try:
        return eng.preview(task_id)
    except GmailAuthError:
        raise ApiError(409, "gmail_disconnected", "The owner's Gmail is not connected") from None
    except GmailUnavailable:
        raise ApiError(503, "gmail_unavailable", "Gmail could not be reached; try again") from None
    except DraftingUnavailable:
        raise ApiError(503, "ai_unavailable", "Drafting is temporarily unavailable; try again") from None


@router.post("/{task_id}/run", status_code=202)
def run(
    task_id: str,
    request: Request,
    background: BackgroundTasks,
    caller: Caller = Depends(require_client),
    db: Session = Depends(get_db),
    eng: FollowupEngine = Depends(engine),
    key: str | None = Depends(idem_key),
) -> JSONResponse:
    queued = []

    def do():
        task = scoped_task(db, caller, task_id, lock=True)
        if task.status in TERMINAL:
            raise ApiError(409, "terminal", f"Task is {task.status}")
        if task.status == "draft_pending":
            raise ApiError(
                409,
                "draft_pending",
                "A draft is already waiting for approval or sending",
            )
        if task.paused:
            raise ApiError(409, "paused", "Task is paused; resume it first")
        reason = eng._ineligible(db, task)
        if reason and task.status in ACTIVE:
            raise ApiError(409, reason, f"Task cannot be checked now ({reason})")
        webhooks.audit(db, "run.requested", task, f"by {caller.client.name}")
        queued.append(task.id)
        owner = db.get(User, task.owner_user_id)
        test = eng.settings.test_mode_allows(owner.email, task.client_email)
        return 202, {
            "task_id": task.id,
            "accepted": True,
            "test_mode": eng.settings.test_mode,
            "message": "Check queued; the waiting interval is skipped"
            + ("" if test else ". Send windows, limits and approval still apply."),
        }

    resp = _mutate(request, db, caller, key, None, do)
    if queued:
        db.commit()
        background.add_task(eng.check_task, queued[0], force=True)
    return resp
