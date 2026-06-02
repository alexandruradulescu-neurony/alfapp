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
