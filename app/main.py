"""FastAPI app: API, OAuth callback and (on one instance) the scheduler."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .ai.draft import ClaudeDrafter, Drafter
from .api import admin, drafts, oauth, tasks, users
from .config import Settings, get_settings
from .core import errors
from .db import init_engine, session_scope
from .engine.check import FollowupEngine
from .gmail.client import GoogleGmail

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
VERSION = "1.1.0"


def create_app(
    settings: Settings | None = None,
    *,
    drafter: Drafter | None = None,
    gmail_factory=None,
    oauth_exchange=None,
    now=None,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.sentry_dsn:
        import sentry_sdk

        # Never send request bodies (email content) to Sentry.
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            send_default_pii=False,
            max_request_body_size="never",
        )
    init_engine(settings.database_url)
    drafter = drafter or ClaudeDrafter(settings.anthropic_api_key, settings.claude_model, settings.company_name)
    gmail_factory = gmail_factory or (
        lambda token: GoogleGmail(token, settings.google_client_id, settings.google_client_secret)
    )
    engine = FollowupEngine(settings, session_scope, gmail_factory, drafter, now=now)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = None
        if settings.scheduler_enabled:
            from .engine.scheduler import build_scheduler

            sched = build_scheduler(engine)
            sched.start()
        yield
        if sched:
            sched.shutdown(wait=False)

    app = FastAPI(title="Follow-Up Agent", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.oauth_exchange = oauth_exchange or oauth.google_exchange
    errors.install(app)
    for r in (tasks.router, drafts.router, users.router, oauth.router, admin.router):
        app.include_router(r)

    @app.get("/v1/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok", "test_mode": settings.test_mode, "version": VERSION}

    return app
