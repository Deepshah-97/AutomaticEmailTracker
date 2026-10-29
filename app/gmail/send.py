"""Send one follow-up as a threaded reply."""

from typing import Any

from .client import GmailApi
from .mime import build_message


def send_followup(
    gmail: GmailApi,
    *,
    sender_email: str,
    sender_name: str,
    to: str,
    subject: str,
    body: str,
    message_id: str,
    thread_id: str | None,
    in_reply_to: str | None,
    references: str | None,
) -> dict[str, Any]:
    raw = build_message(
        sender_email=sender_email,
        sender_name=sender_name,
        to=to,
        subject=subject,
        body=body,
        message_id=message_id,
        in_reply_to=in_reply_to if thread_id else None,
        references=references if thread_id else None,
    )
    return gmail.send(raw, thread_id)


def gmail_link(thread_id: str | None) -> str | None:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}" if thread_id else None
