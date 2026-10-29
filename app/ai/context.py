"""Assemble the drafting context (spec section 7) and a safe summary for the dashboard."""

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ..gmail.thread import Message

SCENARIO_GOALS = {
    "no_reply": "Gently restart the conversation, referencing the last open point.",
    "new_lead": "Respond to their interest and propose a call.",
    "post_demo": "Thank them, recap key points from the notes, ask for feedback and the next step.",
    "proposal_sent": "Check they received the proposal and offer to answer questions.",
    "meeting_no_show": "Offer to reschedule, with no guilt.",
}


@dataclass
class DraftContext:
    scenario: str
    goal: str
    sequence_number: int
    max_followups: int
    owner_name: str
    owner_email: str
    owner_timezone: str
    signature: str
    style_examples: list[str]
    client_name: str
    client_company: str | None
    client_email: str
    notes: str
    thread: list[Message]
    earlier_followups: list[tuple[int, str]]
    thread_subject: str
    today: datetime
    token_budget: int = 12000

    def thread_text(self) -> str:
        """Oldest first, trimmed from the oldest end to roughly `token_budget` tokens (4 chars/token)."""
        chunks = [
            f"From: {m.from_name or m.from_email} <{m.from_email}>\nDate: {m.date:%Y-%m-%d %H:%M} UTC\n\n{m.body}"
            for m in self.thread
            if not m.is_bounce
        ]
        budget = self.token_budget * 4
        kept: list[str] = []
        for chunk in reversed(chunks):
            if sum(len(c) for c in kept) + len(chunk) > budget:
                break
            kept.append(chunk)
        return "\n\n=====\n\n".join(reversed(kept))

    def user_message(self) -> str:
        local_today = self.today.astimezone(ZoneInfo(self.owner_timezone))
        earlier = "\n\n".join(f"Follow-up {n}:\n{b}" for n, b in self.earlier_followups) or "(none)"
        company = f" at {self.client_company}" if self.client_company else ""
        return (
            f"Scenario: {self.scenario}\nGoal: {self.goal}\n"
            f"This is follow-up {self.sequence_number} of {self.max_followups}.\n"
            f"Client: {self.client_name}{company}\n"
            f"Today in {self.owner_name}'s time zone: {local_today:%A %d %B %Y}\n"
            f"Thread subject: {self.thread_subject or '(new thread)'}\n\n"
            f"<thread>\n{self.thread_text() or '(no messages yet)'}\n</thread>\n\n"
            f"<notes>\n{self.notes or '(none)'}\n</notes>\n\n"
            f"<earlier_followups>\n{earlier}\n</earlier_followups>"
        )

    def fact_corpus(self) -> str:
        return "\n".join([self.thread_text(), self.notes, *[b for _, b in self.earlier_followups]])

    def safe_summary(self) -> str:
        """For the dashboard: no raw thread text."""
        last_out = next(
            (m for m in reversed(self.thread) if m.from_email == self.owner_email.lower()),
            None,
        )
        notes = re.sub(r"\s+", " ", self.notes or "").strip()
        parts = [
            f"Scenario {self.scenario}, follow-up {self.sequence_number} of {self.max_followups}.",
            f"Thread: {len(self.thread)} message(s)"
            + (f", last from owner on {last_out.date:%b %d}" if last_out else "")
            + ".",
            f"Notes: {notes[:200] + ('...' if len(notes) > 200 else '') if notes else 'none'}.",
            f"Earlier follow-ups: {len(self.earlier_followups)}.",
        ]
        return " ".join(parts)
