"""RFC 5322 messages for threaded replies."""

import base64
import re
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate


def make_message_id(sender_email: str) -> str:
    domain = sender_email.rsplit("@", 1)[-1]
    return f"<fua.{uuid.uuid4().hex}@{domain}>"


def reply_subject(original: str) -> str:
    original = original.strip()
    return original if re.match(r"^re:", original, re.IGNORECASE) else f"Re: {original}"


def build_message(
    *,
    sender_email: str,
    sender_name: str,
    to: str,
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    """Return the base64url raw message for users.messages.send. Never adds Cc or Bcc."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()
