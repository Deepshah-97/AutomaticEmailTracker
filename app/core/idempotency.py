"""Stored responses for Idempotency-Key retries (A5).

The key is reserved in the same transaction as the change it protects, so a concurrent retry
blocks on the unique index and then replays the committed response.
"""

import hashlib
import json
from collections.abc import Callable
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import IdempotencyRecord
from .errors import ApiError, error_body


def _hash(method: str, path: str, body: Any) -> str:
    return hashlib.sha256(json.dumps([method, path, body], sort_keys=True, default=str).encode()).hexdigest()


def run(
    db: Session,
    client_id: str,
    key: str | None,
    method: str,
    path: str,
    body: Any,
    fn: Callable[[], tuple[int, Any]],
) -> JSONResponse:
    if not key:
        status, payload = _call(fn)
        return JSONResponse(payload, status_code=status)
    if len(key) > 200:
        raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key is too long")
    req_hash = _hash(method, path, body)
    record = IdempotencyRecord(
        api_client_id=client_id,
        key=key,
        method=method,
        path=path,
        request_hash=req_hash,
    )
    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.api_client_id == client_id,
                IdempotencyRecord.key == key,
            )
        )
        if existing is None:  # pragma: no cover - deleted between insert and read
            raise ApiError(409, "idempotency_conflict", "Retry the request") from None
        if existing.request_hash != req_hash:
            raise ApiError(
                422,
                "idempotency_key_reused",
                "This Idempotency-Key was used for a different request",
            ) from None
        if existing.status_code is None:
            raise ApiError(409, "request_in_progress", "The original request is still in progress") from None
        return JSONResponse(
            existing.response,
            status_code=existing.status_code,
            headers={"Idempotent-Replayed": "true"},
        )
    status, payload = _call(fn)
    record.status_code, record.response = status, payload
    return JSONResponse(payload, status_code=status)


def _call(fn: Callable[[], tuple[int, Any]]) -> tuple[int, Any]:
    try:
        return fn()
    except ApiError as exc:
        # Deterministic client errors are stored too, so a retry gets the same answer.
        if exc.status_code < 500:
            return exc.status_code, error_body(exc.code, exc.message)
        raise
