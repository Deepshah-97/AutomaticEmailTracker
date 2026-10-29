"""In-memory stand-ins for Gmail and Claude, used by the tests and the local demo seed."""

import base64
import email
import itertools
import threading
from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any

from app.ai.context import DraftContext
from app.ai.draft import DraftResult
from app.gmail.client import GmailSendRejected, GmailSendUnknown, GmailUnavailable

_ids = itertools.count(1000)


def gmail_message(
    thread_id: str,
    *,
    sender: str,
    to: str,
    subject: str,
    body: str,
    at: datetime,
    message_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
    in_reply_to: str | None = None,
) -> dict[str, Any]:
    mid = f"m{next(_ids)}"
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": message_id or f"<{mid}@mail.test>"},
    ]
    if in_reply_to:
        headers += [
            {"name": "In-Reply-To", "value": in_reply_to},
            {"name": "References", "value": in_reply_to},
        ]
    for k, v in (extra_headers or {}).items():
        headers.append({"name": k, "value": v})
    return {
        "id": mid,
        "threadId": thread_id,
        "labelIds": [],
        "internalDate": str(int(at.timestamp() * 1000)),
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }


class FakeMailbox:
    """All mailboxes share one store; one GmailApi view per owner."""

    def __init__(self) -> None:
        self.threads: dict[str, list[dict]] = {}
        self.sent_raw: list[dict] = []
        self.lock = threading.Lock()
        self.fail_reads = False
        self.send_behavior = "ok"  # ok | timeout_after_accept | timeout_before_accept | rejected
        self.auth_error = False
        self.now = lambda: datetime.now(UTC)

    def add(self, thread_id: str, **kw) -> dict:
        msg = gmail_message(thread_id, **kw)
        self.threads.setdefault(thread_id, []).append(msg)
        return msg

    def view(self, owner_token: str) -> "FakeGmail":
        return FakeGmail(self, owner_token)


class FakeGmail:
    def __init__(self, box: FakeMailbox, token: str) -> None:
        self.box = box
        self.token = token

    def _check(self) -> None:
        from app.gmail.client import GmailAuthError

        if self.box.auth_error:
            raise GmailAuthError("invalid_grant")
        if self.box.fail_reads:
            raise GmailUnavailable("simulated outage")

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        self._check()
        return {"id": thread_id, "messages": list(self.box.threads.get(thread_id, []))}

    def search_threads(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self._check()
        needle = query.split("from:")[-1].split(" ")[0] if "from:" in query else ""
        out = []
        for tid, msgs in self.box.threads.items():
            text = " ".join(h["value"] for m in msgs for h in m["payload"]["headers"])
            if not needle or needle in text:
                out.append({"id": tid, "messages": msgs})
        return out[:limit]

    def send(self, raw: str, thread_id: str | None) -> dict[str, Any]:
        self._check()
        behavior = self.box.send_behavior
        if behavior == "rejected":
            raise GmailSendRejected("400 invalid recipient")
        if behavior == "timeout_before_accept":
            raise GmailSendUnknown("timeout")
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
        tid = thread_id or f"t{next(_ids)}"
        with self.box.lock:
            stored = gmail_message(
                tid,
                sender=msg["From"],
                to=msg["To"],
                subject=msg["Subject"],
                body=msg.get_payload(decode=True).decode(),
                at=self.box.now(),
                message_id=msg["Message-ID"],
                in_reply_to=msg["In-Reply-To"],
            )
            self.box.threads.setdefault(tid, []).append(stored)
            self.box.sent_raw.append(
                {
                    "thread_id": tid,
                    "headers": dict(msg.items()),
                    "body": msg.get_payload(decode=True).decode(),
                    "from": parseaddr(msg["From"])[1],
                }
            )
        if behavior == "timeout_after_accept":
            raise GmailSendUnknown("timeout after submission")
        return {"id": stored["id"], "threadId": tid}

    def find_by_message_id(self, rfc_message_id: str) -> dict[str, Any] | None:
        self._check()
        for tid, msgs in self.box.threads.items():
            for m in msgs:
                for h in m["payload"]["headers"]:
                    if h["name"] == "Message-ID" and h["value"] == rfc_message_id:
                        return {"id": m["id"], "threadId": tid}
        return None


class FakeDrafter:
    """Writes a plain, specific draft from the notes. Can be scripted with `queue`."""

    def __init__(self) -> None:
        self.calls: list[tuple[DraftContext, list[str] | None]] = []
        self.queue: list[DraftResult] = []

    def draft(self, ctx: DraftContext, *, feedback: list[str] | None = None) -> DraftResult:
        self.calls.append((ctx, feedback))
        if self.queue:
            return self.queue.pop(0)
        first = ctx.client_name.split()[0]
        detail = (ctx.notes or "our conversation").split(".")[0].strip()
        lines = {
            1: f"Thanks again for the time. You mentioned {detail.lower()}, so I wanted to make sure you have "
            "what you need. Would a short call this week help?",
            2: f"One more thought on {detail.lower()}: happy to put together a short summary for your team. "
            "Should I send it over?",
        }
        body = (
            f"Hi {first},\n\n{lines.get(ctx.sequence_number, 'Should I check back next quarter instead?')}"
            f"\n\nBest,\n{ctx.owner_name.split()[0]}"
        )
        return DraftResult(
            subject=f"Re: {ctx.thread_subject}" if ctx.thread_subject else "Quick follow-up",
            body=body,
            needs_review=not ctx.notes,
            reason=f"Referenced: {detail}",
            model="fake",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.001,
        )
