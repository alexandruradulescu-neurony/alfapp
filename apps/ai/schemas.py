"""Pydantic output schemas per LLM call site.

Each call site declares the shape it expects the LLM to return; AIClient
validates against the schema before un-tokenizing and returning. Misshapen
replies raise AIResponseValidationError, which callers route to manual review.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, field_validator


def _trim(value: str, limit: int) -> str:
    """Soft length cap: a wordy-but-correct answer gets trimmed, never
    rejected (a hard max_length throws the whole reply away — seen live
    with flight_check, where a good 900-char analysis was discarded)."""
    value = (value or '').strip()
    if len(value) <= limit:
        return value
    return value[:limit - 1].rstrip() + '…'


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


class EvidencePlacement(BaseModel):
    """One evidence record's placement in the dispute report narrative."""

    index: int
    section: Literal[
        "SERVICE_INITIATION",
        "FLIGHT_IDENTIFICATION",
        "INTERACTIONS",
        "SUBMISSIONS",
        "CLAIM_UPDATES",
        "OTHER",
        "EXCLUDE",
    ]
    explanation: str = ""

    @field_validator("explanation")
    @classmethod
    def _cap_explanation(cls, value: str) -> str:
        return _trim(value, 280)


class EvidenceNarrative(BaseModel):
    """Schema for `_narrate_evidence` in payments/document_service.py — the LLM
    sorts each numbered evidence record into a narrative section (or EXCLUDE)
    and writes a one-line relevance note. Facts come only from the record text;
    the LLM never invents content."""

    items: list[EvidencePlacement] = Field(default_factory=list)


class DisputeNarrative(BaseModel):
    """Schema for `build_dispute_narrative_notes` in payments/document_service.py.

    The LLM writes ALF's first-person evidence narrative for a PayPal dispute
    reviewer, in four sections we assemble into the `notes` text submitted to
    PayPal. Facts come ONLY from the case data provided; the LLM never invents
    content. All four fields are required — a malformed/short reply raises
    AIResponseValidationError and the caller falls back to a deterministic
    template narrative (so a bad AI reply never produces empty evidence)."""

    opening: str
    authorization: str
    service_delivery: str
    closing: str


class BriefingSummary(BaseModel):
    """Schema for the Zendesk sidebar briefing (POST /zd/briefing/) and the
    stored claim summary engine. The LLM produces a short summary + a few
    suggested next steps. The structured `facts` block is assembled by the
    view, not the LLM, so it is not part of this schema. The summary is
    soft-capped: trimmed, not rejected (same failure class as flight_check —
    a wordy reply must not cost us a fresh summary)."""

    summary: str
    next_steps: list[str] = Field(default_factory=list, max_length=6)
    delta: str = Field(default='', max_length=400)
    risk_level: Literal['none', 'watch', 'at_risk'] = 'none'
    risk_reasons: list[str] = Field(default_factory=list)
    risk_note: str = Field(default='', max_length=300)

    @field_validator('summary')
    @classmethod
    def _cap_summary(cls, value: str) -> str:
        return _trim(value, 600)


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
    discrepancies (wrong day, airport not on route, etc.). The summary is
    soft-capped: trimmed, not rejected."""

    summary: str
    mismatches: list[str] = Field(default_factory=list, max_length=5)

    @field_validator('summary')
    @classmethod
    def _cap_summary(cls, value: str) -> str:
        return _trim(value, 800)
