"""Turn context into a follow-up email with Claude, returning validated JSON."""

import logging
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from .context import DraftContext
from .prompt import style_block, system_prompt

log = logging.getLogger("agent.ai")

# USD per million tokens: input, output, cache write (5 min), cache read. Check pricing before launch.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.40),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}


class DraftOut(BaseModel):
    subject: str = Field(description="Email subject. When replying in a thread, 'Re: ' plus the thread subject.")
    body: str = Field(description="Plain-text email body including greeting and sign-off.")
    needs_review: bool = Field(description="True if the context is too thin or something looks off.")
    reason: str = Field(description="One sentence: which specific details were referenced.")


@dataclass
class DraftResult:
    subject: str
    body: str
    needs_review: bool
    reason: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class Drafter(Protocol):
    def draft(self, ctx: DraftContext, *, feedback: list[str] | None = None) -> DraftResult: ...


class DraftingUnavailable(Exception):
    pass


class ClaudeDrafter:
    def __init__(self, api_key: str, model: str, company_name: str, effort: str = "medium") -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key or None, max_retries=2, timeout=90.0)
        self.model = model
        self.company_name = company_name
        self.effort = effort

    def draft(self, ctx: DraftContext, *, feedback: list[str] | None = None) -> DraftResult:
        anthropic = self._anthropic
        user = ctx.user_message()
        if feedback:
            user += "\n\nYour previous draft failed these checks; write a new one that passes them:\n- " + "\n- ".join(
                feedback
            )
        # Stable prefix first so it caches: rules, then this owner's style examples.
        system = [
            {"type": "text", "text": system_prompt(ctx.owner_name, self.company_name)},
            {
                "type": "text",
                "text": style_block(ctx.style_examples, ctx.signature),
                "cache_control": {"type": "ephemeral"},
            },
        ]
        try:
            resp = self._client.beta.messages.parse(
                model=self.model,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=DraftOut,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ) as exc:
            raise DraftingUnavailable(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise DraftingUnavailable(str(exc)) from exc
            raise

        usage = resp.usage
        cost = self._cost(resp.model or self.model, usage)
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            return DraftResult(
                subject="",
                body="",
                needs_review=True,
                reason=f"Model did not produce a draft (stop_reason={resp.stop_reason})",
                model=resp.model or self.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=cost,
            )
        out: DraftOut = resp.parsed_output
        return DraftResult(
            subject=out.subject.strip(),
            body=out.body.strip(),
            needs_review=out.needs_review,
            reason=out.reason.strip(),
            model=resp.model or self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=cost,
        )

    def _cost(self, model: str, usage) -> float:
        p_in, p_out, p_write, p_read = PRICES.get(model, PRICES["claude-sonnet-5-5"])
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        return (
            usage.input_tokens * p_in + usage.output_tokens * p_out + cache_write * p_write + cache_read * p_read
        ) / 1_000_000
