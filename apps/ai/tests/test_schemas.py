import pytest
from pydantic import ValidationError
from apps.ai.schemas import (
    EmailCategorization,
    TicketExtraction,
    ChatAnswer,
    DisputeLetter,
)


# ---- EmailCategorization ----

def test_email_categorization_accepts_valid_payload():
    obj = EmailCategorization.model_validate({
        "summary": "Bag found at JFK",
        "category": "OBJECT_FOUND",
        "action_required": False,
        "auto_resolvable": True,
    })
    assert obj.category == "OBJECT_FOUND"


def test_email_categorization_rejects_invented_category():
    with pytest.raises(ValidationError):
        EmailCategorization.model_validate({
            "summary": "Bag found at JFK",
            "category": "REFUND_NEEDED",  # not in the Literal set
            "action_required": False,
            "auto_resolvable": True,
        })


def test_email_categorization_rejects_too_long_summary():
    with pytest.raises(ValidationError):
        EmailCategorization.model_validate({
            "summary": "x" * 501,
            "category": "UNKNOWN",
            "action_required": False,
            "auto_resolvable": False,
        })


# ---- TicketExtraction ----

def test_ticket_extraction_all_fields_optional():
    obj = TicketExtraction.model_validate({})
    assert obj.object_description is None
    assert obj.additional_context is None


def test_ticket_extraction_does_not_have_flight_details():
    """flight_details is read from structured Zendesk fields, not extracted by LLM."""
    fields = TicketExtraction.model_fields
    assert "flight_details" not in fields, \
        "TicketExtraction should NOT have flight_details — read from structured Zendesk custom field instead"
    assert "object_description" in fields
    assert "additional_context" in fields


# ---- ChatAnswer ----

def test_chat_answer_requires_answer():
    with pytest.raises(ValidationError):
        ChatAnswer.model_validate({"sources": []})


def test_chat_answer_caps_answer_length():
    with pytest.raises(ValidationError):
        ChatAnswer.model_validate({"answer": "x" * 2001, "sources": []})


def test_chat_answer_rejects_unknown_source():
    with pytest.raises(ValidationError):
        ChatAnswer.model_validate({
            "answer": "ok",
            "sources": ["claim", "wikipedia"],  # wikipedia not in Literal
        })


# ---- DisputeLetter ----

def test_dispute_letter_caps_body_length():
    with pytest.raises(ValidationError):
        DisputeLetter.model_validate({
            "subject": "Response to dispute",
            "body": "x" * 5001,
        })


def test_dispute_letter_caps_subject_length():
    with pytest.raises(ValidationError):
        DisputeLetter.model_validate({
            "subject": "x" * 201,
            "body": "ok",
        })


# ---- BriefingSummary ----

from apps.ai.schemas import BriefingSummary


def test_briefing_summary_accepts_valid_payload():
    obj = BriefingSummary.model_validate({
        "summary": "Bag lost on UA123; searching 9 days.",
        "next_steps": ["Chase airport", "Send 11-day update"],
    })
    assert obj.summary.startswith("Bag lost")
    assert len(obj.next_steps) == 2


def test_briefing_summary_next_steps_defaults_empty():
    obj = BriefingSummary.model_validate({"summary": "All quiet."})
    assert obj.next_steps == []


def test_briefing_summary_trims_too_long_summary():
    # Contract changed 2026-06-11: soft cap. A wordy reply is trimmed, never
    # rejected — rejection cost us fresh summaries in production.
    obj = BriefingSummary.model_validate({"summary": "x" * 601})
    assert len(obj.summary) <= 600
    assert obj.summary.endswith("…")


def test_briefing_summary_caps_next_steps_count():
    with pytest.raises(ValidationError):
        BriefingSummary.model_validate({
            "summary": "ok",
            "next_steps": [f"step {i}" for i in range(7)],
        })


def test_next_steps_accepts_valid_payload():
    from apps.ai.schemas import NextSteps
    obj = NextSteps.model_validate({"next_steps": ["Chase MCO lost & found", "Update client"]})
    assert len(obj.next_steps) == 2


def test_next_steps_caps_count():
    import pytest
    from pydantic import ValidationError
    from apps.ai.schemas import NextSteps
    with pytest.raises(ValidationError):
        NextSteps.model_validate({"next_steps": [f"step {i}" for i in range(7)]})


def test_next_steps_requires_field():
    import pytest
    from pydantic import ValidationError
    from apps.ai.schemas import NextSteps
    with pytest.raises(ValidationError):
        NextSteps.model_validate({})


def test_email_draft_accepts_valid_payload():
    from apps.ai.schemas import EmailDraft
    obj = EmailDraft.model_validate({"body": "Dear client, your wallet was found."})
    assert obj.body.startswith("Dear client")


def test_email_draft_rejects_too_long_body():
    import pytest
    from pydantic import ValidationError
    from apps.ai.schemas import EmailDraft
    with pytest.raises(ValidationError):
        EmailDraft.model_validate({"body": "x" * 4001})


def test_email_draft_requires_body():
    import pytest
    from pydantic import ValidationError
    from apps.ai.schemas import EmailDraft
    with pytest.raises(ValidationError):
        EmailDraft.model_validate({})


def test_flight_check_trims_long_summary_instead_of_rejecting():
    # Seen live: a correct ~900-char flight analysis was thrown away by a hard
    # max_length. Long summaries must validate, trimmed with an ellipsis.
    from apps.ai.schemas import FlightCheck
    obj = FlightCheck.model_validate({"summary": "x" * 1000, "mismatches": []})
    assert len(obj.summary) <= 800
    assert obj.summary.endswith("…")


def test_flight_check_short_summary_untouched():
    from apps.ai.schemas import FlightCheck
    obj = FlightCheck.model_validate({"summary": "All good.", "mismatches": []})
    assert obj.summary == "All good."


def test_briefing_summary_trims_long_summary():
    from apps.ai.schemas import BriefingSummary
    obj = BriefingSummary.model_validate({"summary": "y" * 900})
    assert len(obj.summary) <= 600
    assert obj.summary.endswith("…")
