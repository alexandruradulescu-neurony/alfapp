import pytest
from apps.ai.exceptions import AIClientError, AIResponseValidationError


def test_ai_client_error_is_exception():
    """AIClientError is a regular Exception subclass."""
    err = AIClientError("something broke")
    assert isinstance(err, Exception)
    assert str(err) == "something broke"


def test_ai_response_validation_error_carries_context():
    """AIResponseValidationError carries call_site and raw_reply for debugging."""
    err = AIResponseValidationError(
        call_site="email_categorizer",
        raw_reply='{"bad": "shape"}',
        message="expected EmailCategorization",
    )
    assert err.call_site == "email_categorizer"
    assert err.raw_reply == '{"bad": "shape"}'
    assert "email_categorizer" in str(err)
    assert isinstance(err, AIClientError)


def test_ai_response_validation_error_truncates_long_raw_reply_in_repr():
    """A 10KB raw reply doesn't blow up the str() representation."""
    huge = "x" * 10_000
    err = AIResponseValidationError(
        call_site="chat_agent",
        raw_reply=huge,
        message="parsing failed",
    )
    rendered = str(err)
    assert len(rendered) < 2_000  # truncated, not full huge reply
    assert "chat_agent" in rendered
