"""Parse Gmail threads and decide whether the client replied (A3)."""

import base64
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import getaddresses, parseaddr

OPT_OUT = re.compile(
    r"\b(unsubscribe|not interested|please stop|stop (emailing|contacting|sending)|remove me|"
    r"take me off|do not (contact|email) me|don't (contact|email) me)\b",
    re.IGNORECASE,
)
AUTO_SUBJECT = re.compile(
    r"^(automatic reply|auto[- ]?reply|out of (the )?office|ooo\b|away from)",
    re.IGNORECASE,
)
BOUNCE_SENDER = re.compile(r"^(mailer-daemon|postmaster)@", re.IGNORECASE)
BOUNCE_SUBJECT = re.compile(
    r"(delivery status notification|undeliverable|undelivered mail|returned mail|"
    r"delivery (has )?failed|failure notice)",
    re.IGNORECASE,
)
QUOTE_HEADER = re.compile(r"^\s*(On .+wrote:|-{2,} ?Original Message ?-{2,}|From: .+)$", re.IGNORECASE)
SIGNATURE = re.compile(r"^(-- ?|—|Sent from my \w+)", re.IGNORECASE)


@dataclass
class Message:
    id: str
    rfc_message_id: str | None
    from_email: str
    from_name: str
    to: list[str]
    cc: list[str]
    date: datetime
    subject: str
    body: str
    references: str | None = None
    in_reply_to: str | None = None
    labels: list[str] = field(default_factory=list)
    auto_submitted: bool = False

    @property
    def is_bounce(self) -> bool:
        return bool(BOUNCE_SENDER.match(self.from_email) or BOUNCE_SUBJECT.search(self.subject))

    @property
    def is_auto_reply(self) -> bool:
        return self.auto_submitted or bool(AUTO_SUBJECT.match(self.subject))


def _header(headers: list[dict], name: str) -> str | None:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _plain_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        text = _plain_text(part)
        if text:
            return text
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        return re.sub(r"<[^>]+>", " ", _decode(payload["body"]["data"]))
    return ""


def strip_quotes_and_signature(text: str) -> str:
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if QUOTE_HEADER.match(line) or SIGNATURE.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def parse_message(raw: dict) -> Message:
    headers = raw.get("payload", {}).get("headers", [])
    name, addr = parseaddr(_header(headers, "From") or "")
    auto = (_header(headers, "Auto-Submitted") or "no").lower() != "no" or bool(
        _header(headers, "X-Autoreply") or _header(headers, "X-Autorespond")
    )
    precedence = (_header(headers, "Precedence") or "").lower()
    return Message(
        id=raw["id"],
        rfc_message_id=_header(headers, "Message-ID") or _header(headers, "Message-Id"),
        from_email=addr.lower(),
        from_name=name,
        to=[a.lower() for _, a in getaddresses([_header(headers, "To") or ""]) if a],
        cc=[a.lower() for _, a in getaddresses([_header(headers, "Cc") or ""]) if a],
        date=datetime.fromtimestamp(int(raw.get("internalDate", "0")) / 1000, UTC),
        subject=_header(headers, "Subject") or "",
        body=strip_quotes_and_signature(_plain_text(raw.get("payload", {}))),
        references=_header(headers, "References"),
        in_reply_to=_header(headers, "In-Reply-To"),
        labels=list(raw.get("labelIds", [])),
        auto_submitted=auto or precedence in {"auto_reply", "bulk", "junk"},
    )


def parse_thread(raw: dict) -> list[Message]:
    return sorted((parse_message(m) for m in raw.get("messages", [])), key=lambda m: m.date)


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


@dataclass
class ThreadState:
    messages: list[Message]
    last_owner_outbound: Message | None
    client_reply: Message | None = None
    opt_out: Message | None = None
    ambiguous: str | None = None
    ignored: list[str] = field(default_factory=list)

    @property
    def subject(self) -> str:
        first = next((m.subject for m in self.messages if m.subject), "")
        return re.sub(r"^(re|fwd?):\s*", "", first, flags=re.IGNORECASE).strip()

    @property
    def last_message(self) -> Message | None:
        return self.messages[-1] if self.messages else None


def analyze(
    messages: list[Message],
    owner_email: str,
    client_email: str,
    *,
    since: datetime | None = None,
    extra_participants: set[str] | None = None,
) -> ThreadState:
    """Decide whether a meaningful client reply exists after the owner's latest outbound message.

    * Inbound = messages after max(last owner outbound, `since`) not sent by the owner.
    * Auto-replies and bounces are never treated as human replies.
    * An explicit opt-out is an immediate stop.
    * Anything from an unknown participant is ambiguous and requires review.
    """
    owner, client = owner_email.lower(), client_email.lower()
    approved = {client} | {p.lower() for p in (extra_participants or set())}
    client_domain = _domain(client)
    owner_domain = _domain(owner)
    owner_msgs = [m for m in messages if m.from_email == owner]
    last_out = owner_msgs[-1] if owner_msgs else None
    cutoff = max(
        [d for d in (last_out.date if last_out else None, since) if d is not None],
        default=None,
    )
    state = ThreadState(messages=messages, last_owner_outbound=last_out)
    for m in messages:
        if m.from_email == owner or (cutoff is not None and m.date <= cutoff):
            continue
        if m.is_bounce:
            state.ignored.append(f"bounce {m.id}")
            continue
        if OPT_OUT.search(m.body) or OPT_OUT.search(m.subject):
            if m.from_email in approved or _domain(m.from_email) == client_domain:
                state.opt_out = m
                return state
        if m.is_auto_reply:
            state.ignored.append(f"auto-reply {m.id}")
            continue
        if m.from_email in approved or _domain(m.from_email) == client_domain:
            state.client_reply = m
            return state
        if _domain(m.from_email) == owner_domain:
            state.ambiguous = f"A colleague ({m.from_email}) wrote after the last tracked message"
        else:
            state.ambiguous = f"Unknown participant {m.from_email} wrote in the thread"
    if cutoff is None and messages and not last_out:
        state.ambiguous = state.ambiguous or "No message from the owner in this thread"
    return state


def summarize(meta: dict) -> dict:
    """thread_id, subject, last_sender, date from a metadata-format thread."""
    msgs = parse_thread(meta)
    last = msgs[-1] if msgs else None
    return {
        "thread_id": meta["id"],
        "subject": next((m.subject for m in msgs if m.subject), "(no subject)"),
        "last_sender": last.from_email if last else "",
        "date": last.date.isoformat().replace("+00:00", "Z") if last else None,
    }
