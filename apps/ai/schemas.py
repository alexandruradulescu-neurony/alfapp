"""Pydantic output schemas per LLM call site.

Each call site declares the shape it expects the LLM to return; AIClient
validates against the schema before un-tokenizing and returning. Misshapen
replies raise AIResponseValidationError, which callers route to manual review.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class EmailCategorization(BaseModel):
    """Schema for `call_qwen_ai` (email categorizer) in
    apps/communications/services.py."""

    summary: str = Field(max_length=500)
    category: Literal[
        "OBJECT_FOUND",
        "OBJECT_NOT_FOUND",
        "RESUBMISSION_REQUIRED",
        "SUBMISSION_CONFIRMATION",
        "GENERAL_CORRESPONDENCE",
        "UNKNOWN",
    ]
    action_required: bool
    auto_resolvable: bool


class TicketExtraction(BaseModel):
    """Schema for `call_qwen_ai_for_ticket_extraction`. The LLM only handles
    free-text fields; structured Zendesk custom fields (name, email, phone,
    flight) are read directly from the ticket payload."""

    object_description: str | None = None
    additional_context: str | None = None


class ChatAnswer(BaseModel):
    """Schema for `AgentChatService._call_llm` (manager LLM chat)."""

    answer: str = Field(max_length=2000)
    sources: list[Literal["claim", "email", "refund", "zendesk"]] = []


class DisputeLetter(BaseModel):
    """Schema for `_call_qwen_ai` in payments/document_service.py."""

    subject: str = Field(max_length=200)
    body: str = Field(max_length=5000)


class BriefingSummary(BaseModel):
    """Schema for the Zendesk sidebar briefing (POST /zd/briefing/).
    The LLM produces a short summary + a few suggested next steps. The
    structured `facts` block is assembled by the view, not the LLM, so it is
    not part of this schema."""

    summary: str = Field(max_length=600)
    next_steps: list[str] = Field(default_factory=list, max_length=6)


class NextSteps(BaseModel):
    """Schema for the Zendesk sidebar 'Generate next steps' action
    (POST /zd/briefing/ with mode='next_steps')."""

    next_steps: list[str] = Field(max_length=6)


class EmailDraft(BaseModel):
    """Schema for the Zendesk sidebar draft endpoint (POST /zd/draft/).
    Body only — the draft is inserted into the ticket's existing reply box."""

    body: str = Field(max_length=4000)


class FlightCheck(BaseModel):
    """Schema for the flight-lookup AI cross-check (POST /zd/flight-lookup/).
    Validates fetched flight data (or candidate flights) against the client's
    report and says where the search should focus; `mismatches` lists concrete
    discrepancies (wrong day, airport not on route, etc.)."""

    summary: str = Field(max_length=600)
    mismatches: list[str] = Field(default_factory=list, max_length=5)
