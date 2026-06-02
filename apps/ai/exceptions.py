"""Exceptions for the AI client layer."""

from __future__ import annotations


class AIClientError(Exception):
    """Base class for all AI client errors."""


class AIResponseValidationError(AIClientError):
    """The LLM's reply did not match the caller's expected Pydantic schema.

    Carries enough context for the caller to log the bad reply and route to
    a manual-review queue.
    """

    _RAW_REPLY_MAX = 1500

    def __init__(
        self,
        *,
        call_site: str,
        raw_reply: str,
        message: str = "LLM response did not match expected schema",
    ) -> None:
        self.call_site = call_site
        self.raw_reply = raw_reply
        self.message = message
        super().__init__(self._render())

    def _render(self) -> str:
        truncated = self.raw_reply
        if len(truncated) > self._RAW_REPLY_MAX:
            truncated = truncated[: self._RAW_REPLY_MAX] + f"... [truncated, {len(self.raw_reply)} chars total]"
        return f"[{self.call_site}] {self.message} | raw_reply={truncated!r}"
