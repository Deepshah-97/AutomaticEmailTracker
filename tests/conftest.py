import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.config import Settings
from app.core.crypto import generate_key
from app.main import create_app
from app.models import Base

from .fakes import FakeDrafter, FakeMailbox

ADMIN = "admin-key-for-tests"
BASE_DB = os.environ.get(
    "TEST_DATABASE_ADMIN_URL",
    "postgresql+psycopg://agent:agent@localhost:5432/followup",
)
TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://agent:agent@localhost:5432/followup_test")

OWNER = "sam@company.com"
CLIENT = "priya@acme.io"
# Wednesday 28 Oct 2026, 10:00 New York (14:00 UTC): inside the send window.
WED_10AM = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def database() -> str:
    admin = create_engine(BASE_DB, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS followup_test WITH (FORCE)"))
        c.execute(text("CREATE DATABASE followup_test"))
    admin.dispose()
    from alembic.config import Config

    from alembic import command

    os.environ["DATABASE_URL"] = TEST_DB
    from app.config import get_settings

    get_settings.cache_clear()
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    command.upgrade(cfg, "head")  # the real migration, not create_all
    return TEST_DB


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


class Env:
    def __init__(
        self,
        client: TestClient,
        box: FakeMailbox,
        drafter: FakeDrafter,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self.c = client
        self.box = box
        self.drafter = drafter
        self.clock = clock
        self.settings = settings
        self.app = client.app
        self.engine = client.app.state.engine
        self.key = ""
        self.webhook_secret = ""
        self.client_id = ""
        self.owner_id = ""

    def h(self, idem: str | None = None, key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {key or self.key}"}
        if idem:
            headers["Idempotency-Key"] = idem
        return headers

    def admin(self, method: str, path: str, **kw):
        return self.c.request(method, path, headers={"Authorization": f"Bearer {ADMIN}"}, **kw)

    def connect_owner(self, user_id: str | None = None) -> None:
        from app.db import session_scope
        from app.models import User

        with session_scope() as db:
            u = db.get(User, user_id or self.owner_id)
            u.google_refresh_token = self.engine.cipher.encrypt(f"refresh-{u.email}")
            u.gmail_status = "connected"

    def seed_thread(
        self,
        thread_id: str = "thr_1",
        *,
        client_replied: bool = False,
        at: datetime | None = None,
        subject: str = "Product demo",
    ) -> str:
        at = at or (self.clock() - timedelta(days=5))
        m1 = self.box.add(
            thread_id,
            sender=f"Sam Sender <{OWNER}>",
            to=CLIENT,
            subject=subject,
            body="Hi Priya, great to meet you. Here is the demo link.",
            at=at,
        )
        m2 = self.box.add(
            thread_id,
            sender=f"Priya Shah <{CLIENT}>",
            to=OWNER,
            subject=f"Re: {subject}",
            body="Thanks! Does it support SSO? We have 40 seats.",
            at=at + timedelta(hours=2),
            in_reply_to=m1["payload"]["headers"][3]["value"],
        )
        self.box.add(
            thread_id,
            sender=f"Sam Sender <{OWNER}>",
            to=CLIENT,
            subject=f"Re: {subject}",
            body="Yes, SSO is supported. I will send the pricing sheet.",
            at=at + timedelta(hours=3),
            in_reply_to=m2["payload"]["headers"][3]["value"],
        )
        if client_replied:
            self.client_reply(thread_id, at=at + timedelta(hours=5))
        return thread_id

    def client_reply(
        self,
        thread_id: str = "thr_1",
        *,
        body: str = "Sounds good, let's talk Thursday.",
        at: datetime | None = None,
        sender: str = f"Priya Shah <{CLIENT}>",
        **kw,
    ) -> None:
        self.box.add(
            thread_id,
            sender=sender,
            to=OWNER,
            subject="Re: Product demo",
            body=body,
            at=at or self.clock(),
            **kw,
        )

    def create_task(self, idem: str | None = None, **overrides) -> dict:
        body = {
            "external_id": overrides.pop("external_id", "TRK-1042"),
            "owner_email": OWNER,
            "scenario": "post_demo",
            "client": {"name": "Priya Shah", "email": CLIENT, "company": "Acme"},
            "gmail_thread_id": "thr_1",
            "notes": "Liked the reporting view. Asked about SSO. Promised pricing sheet.",
            "scenario_trigger_at": (self.clock() - timedelta(days=3)).isoformat(),
            "trigger_type": "demo_completed_at",
            "send_mode": "approve",
        } | overrides
        r = self.c.post("/v1/tasks", json=body, headers=self.h(idem))
        assert r.status_code == 201, r.text
        return r.json()

    def task(self, task_id: str) -> dict:
        return self.c.get(f"/v1/tasks/{task_id}", headers=self.h()).json()

    def drafts(self) -> list[dict]:
        return self.c.get("/v1/drafts", headers=self.h()).json()["data"]

    def events(self, task_id: str | None = None) -> list[dict]:
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import Event

        with session_scope() as db:
            q = select(Event).where(Event.delivery_status != "none").order_by(Event.id)
            if task_id:
                q = q.where(Event.task_id == task_id)
            return [e.payload for e in db.scalars(q)]

    def event_names(self, task_id: str | None = None) -> list[str]:
        return [e["event"] for e in self.events(task_id)]


def make_settings(**overrides) -> Settings:
    base = dict(
        database_url=TEST_DB,
        token_encryption_key=generate_key(),
        admin_api_key=ADMIN,
        google_client_id="cid.apps.googleusercontent.com",
        google_client_secret="secret",
        company_name="Company",
        sending_enabled=True,
        random_delay_max_minutes=0,
        google_allowed_domains="company.com",
    )
    return Settings(**(base | overrides))


@pytest.fixture
def make_env(database) -> Iterator:
    clients = []

    def _make(**overrides) -> Env:
        from app.core.auth import rate_limiter

        rate_limiter._hits.clear()
        engine = create_engine(database)
        with engine.begin() as c:
            names = ", ".join(t.name for t in Base.metadata.sorted_tables)
            c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        engine.dispose()
        clock = Clock(overrides.pop("now", WED_10AM))
        box = FakeMailbox()
        box.now = clock
        drafter = FakeDrafter()
        settings = make_settings(**overrides)
        app = create_app(settings, drafter=drafter, gmail_factory=box.view, now=clock)
        tc = TestClient(app)
        tc.__enter__()
        clients.append(tc)
        env = Env(tc, box, drafter, clock, settings)
        u = env.admin("POST", "/v1/admin/users", json={"email": OWNER, "name": "Sam Sender"}).json()
        env.owner_id = u["id"]
        cl = env.admin(
            "POST",
            "/v1/admin/api-clients",
            json={
                "name": "Demo dashboard",
                "webhook_url": "https://dash.example.com/webhooks/agent",
                "user_emails": [OWNER],
            },
        ).json()
        env.key, env.webhook_secret, env.client_id = (
            cl["api_key"],
            cl["webhook_secret"],
            cl["id"],
        )
        env.connect_owner()
        return env

    yield _make
    for tc in clients:
        tc.__exit__(None, None, None)


@pytest.fixture
def env(make_env) -> Env:
    return make_env()
