"""Deterministic draft safety checks (spec section 6), applied to AI and human-edited drafts."""

import re

PLACEHOLDER = re.compile(r"\[[^\]\n]{1,40}\]|\{[^}\n]{1,40}\}|<<[^>]+>>|\bXX+\b|\bTBD\b")
AI_MENTION = re.compile(
    r"\b(as an ai|ai[- ]generated|language model|automated (email|message|follow[- ]?up)|"
    r"this (email|message) (was|is) automated|written by (an )?ai|i am a bot|i'm a bot|\bchatgpt\b|\bclaude\b)",
    re.IGNORECASE,
)
BANNED_OPENERS = re.compile(
    r"^(i hope this (email|message) finds you well|just circling back|touching base|i wanted to follow up)",
    re.IGNORECASE,
)
BANNED_ANYWHERE = re.compile(r"don'?t hesitate to reach out", re.IGNORECASE)
MONEY = re.compile(
    r"(?:[$€£]\s?\d[\d,.]*\s?[kKmM]?)|(?:\b\d[\d,.]*\s?(?:usd|eur|gbp|dollars|euros)\b)",
    re.IGNORECASE,
)
PERCENT = re.compile(r"\b\d{1,3}(?:\.\d+)?\s?%")
DISCOUNT = re.compile(
    r"\b(discount|% off|free (month|trial|upgrade)|special (price|offer|rate))\b",
    re.IGNORECASE,
)
MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
DATE = re.compile(
    rf"\b(?:(?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})|"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
COMMITMENT = re.compile(
    r"\b(we (will|can) (guarantee|refund|waive)|guaranteed|money[- ]back|i promise)\b",
    re.IGNORECASE,
)


def _norm(token: str) -> str:
    return re.sub(r"[\s,]", "", token.lower()).rstrip(".")


def _subject_core(subject: str) -> str:
    return re.sub(r"^((re|fwd?|aw):\s*)+", "", subject.strip(), flags=re.IGNORECASE).strip().lower()


def extract_facts(text: str) -> list[str]:
    """Money, percentages and dates present in the source context (kept to validate edits later)."""
    found = MONEY.findall(text) + PERCENT.findall(text) + DATE.findall(text)
    return sorted({_norm(f) for f in found})


def check(
    *,
    subject: str,
    body: str,
    recipient: str,
    client_email: str,
    thread_subject: str | None,
    allowed_facts: list[str],
    fact_corpus: str = "",
) -> list[str]:
    failures: list[str] = []
    if recipient.strip().lower() != client_email.strip().lower():
        failures.append("recipient does not match the task's client email")
    if not body.strip():
        failures.append("draft is empty")
    if PLACEHOLDER.search(subject) or PLACEHOLDER.search(body):
        failures.append("placeholder text left in draft")
    words = len(body.split())
    if words > 150:
        failures.append(f"draft is {words} words (limit 150)")
    if thread_subject and _subject_core(subject) != _subject_core(thread_subject):
        failures.append("subject does not match the thread subject")
    if AI_MENTION.search(body) or AI_MENTION.search(subject):
        failures.append("draft mentions AI or automation")
    opening = [ln.strip() for ln in body.strip().splitlines() if ln.strip()][:2]
    if any(BANNED_OPENERS.search(ln) for ln in opening):
        failures.append("draft uses a banned opening line")
    if BANNED_ANYWHERE.search(body):
        failures.append("draft uses a banned phrase")
    allowed = set(allowed_facts) | set(extract_facts(fact_corpus))
    corpus_lower = fact_corpus.lower()
    invented = [t for t in MONEY.findall(body) + PERCENT.findall(body) if _norm(t) not in allowed]
    if invented:
        failures.append(f"price or number not in thread or notes: {', '.join(sorted(set(invented)))}")
    dates = [d for d in DATE.findall(body) if _norm(d) not in allowed]
    if dates:
        failures.append(f"date not in thread or notes: {', '.join(sorted(set(dates)))}")
    for m in DISCOUNT.finditer(body):
        if m.group(0).lower() not in corpus_lower:
            failures.append(f"offer not in thread or notes: {m.group(0)}")
    for m in COMMITMENT.finditer(body):
        if m.group(0).lower() not in corpus_lower:
            failures.append(f"commitment not in thread or notes: {m.group(0)}")
    return failures
