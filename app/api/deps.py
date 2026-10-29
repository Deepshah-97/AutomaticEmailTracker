from fastapi import Request

from ..engine.check import FollowupEngine


def engine(request: Request) -> FollowupEngine:
    return request.app.state.engine


def idem_key(request: Request) -> str | None:
    return request.headers.get("idempotency-key")
