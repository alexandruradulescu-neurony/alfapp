"""Tests for AIClient.complete — the public orchestration entry point."""

import pytest
from unittest.mock import patch, MagicMock
from pydantic import BaseModel, Field
from typing import Literal

from apps.ai.client import AIClient
from apps.ai.exceptions import AIResponseValidationError


class _DummyReply(BaseModel):
    """Test schema used by these tests only."""
    category: Literal["A", "B"]
    note: str = Field(max_length=200)


def _mock_openai_response(content: str):
    """Build a mock OpenAI ChatCompletion response with the given message content."""
    mock = MagicMock()
    mock.choices = [MagicMock()]
    mock.choices[0].message = MagicMock()
    mock.choices[0].message.content = content
    return mock


@pytest.fixture
def fake_settings(monkeypatch, db):
    """Configure SystemSettings + Django settings for AIClient."""
    from apps.config.models import SystemSettings
    settings_obj, _ = SystemSettings.objects.get_or_create(
        pk=1,
        defaults={
            'ai_api_key': 'test_api_key',
            'ai_api_base': 'https://api.example.com/v1',
            'ai_api_model': 'qwen-turbo',
            'pii_tokenization_salt': 'test_salt_long_enough_for_use',
        },
    )
    # Update salt in case settings_obj already existed
    settings_obj.pii_tokenization_salt = 'test_salt_long_enough_for_use'
    settings_obj.ai_api_key = 'test_api_key'
    settings_obj.ai_api_base = 'https://api.example.com/v1'
    settings_obj.ai_api_model = 'qwen-turbo'
    settings_obj.save()
    return settings_obj


@pytest.mark.django_db
def test_complete_returns_validated_typed_object(fake_settings):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        instance = MockOpenAI.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(
            '{"category": "A", "note": "ok"}'
        )

        result = AIClient.complete(
            system_prompt="You are a test.",
            trusted=None,
            untrusted={"email_body": "hello world"},
            response_schema=_DummyReply,
            call_site="test",
        )
    assert isinstance(result, _DummyReply)
    assert result.category == "A"
    assert result.note == "ok"


@pytest.mark.django_db
def test_complete_strips_markdown_code_fence(fake_settings):
    """Models that wrap JSON in ```json ... ``` must still validate (Qwen does)."""
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        instance = MockOpenAI.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(
            '```json\n{"category": "A", "note": "ok"}\n```'
        )

        result = AIClient.complete(
            system_prompt="You are a test.",
            trusted=None,
            untrusted={"email_body": "hello"},
            response_schema=_DummyReply,
            call_site="test",
        )
    assert result.category == "A"
    assert result.note == "ok"


@pytest.mark.django_db
def test_complete_raises_validation_error_on_bad_output(fake_settings):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        instance = MockOpenAI.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(
            '{"category": "INVALID", "note": "ok"}'  # category not in Literal
        )

        with pytest.raises(AIResponseValidationError) as excinfo:
            AIClient.complete(
                system_prompt="You are a test.",
                trusted=None,
                untrusted={"email_body": "hello"},
                response_schema=_DummyReply,
                call_site="test",
            )
        assert excinfo.value.call_site == "test"
        assert "INVALID" in excinfo.value.raw_reply


@pytest.mark.django_db
def test_complete_tokenizes_pii_before_sending(fake_settings):
    """The string passed to the LLM must contain placeholders, not real emails."""
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        instance = MockOpenAI.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(
            '{"category": "A", "note": "ok"}'
        )

        AIClient.complete(
            system_prompt="You are a test.",
            trusted=None,
            untrusted={"email_body": "Contact alice@example.com please"},
            response_schema=_DummyReply,
            call_site="test",
        )

        # Inspect what was sent to OpenAI
        call_args = instance.chat.completions.create.call_args
        messages = call_args.kwargs["messages"]
        user_content = messages[1]["content"]
        assert "alice@example.com" not in user_content
        assert "<EMAIL_" in user_content


@pytest.mark.django_db
def test_complete_untokenizes_pii_in_response(fake_settings):
    """If the LLM echoes a placeholder back, the returned object has the real value."""
    class EchoReply(BaseModel):
        echoed: str

    # First call tokenizes; we capture the placeholder, then mock the LLM to echo it.
    captured_placeholder = {}

    def capture_then_respond(*args, **kwargs):
        user_content = kwargs["messages"][1]["content"]
        import re
        match = re.search(r"<EMAIL_[a-f0-9]{8}>", user_content)
        captured_placeholder['ph'] = match.group(0)
        return _mock_openai_response(f'{{"echoed": "{captured_placeholder["ph"]}"}}')

    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        instance = MockOpenAI.return_value
        instance.chat.completions.create.side_effect = capture_then_respond

        result = AIClient.complete(
            system_prompt="Echo back the email.",
            trusted=None,
            untrusted={"email_body": "From alice@example.com"},
            response_schema=EchoReply,
            call_site="test",
        )
    assert result.echoed == "alice@example.com"


@pytest.mark.django_db
def test_complete_includes_defense_preamble_in_system_prompt(fake_settings):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        instance = MockOpenAI.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(
            '{"category": "A", "note": "ok"}'
        )

        AIClient.complete(
            system_prompt="You are a test classifier.",
            trusted=None,
            untrusted={"email_body": "x"},
            response_schema=_DummyReply,
            call_site="test",
        )

        system_content = instance.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert "You are a test classifier." in system_content
        assert "SECURITY NOTE" in system_content


@pytest.mark.django_db
def test_complete_uses_known_aliases_in_tokenization(fake_settings):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        instance = MockOpenAI.return_value
        instance.chat.completions.create.return_value = _mock_openai_response(
            '{"category": "A", "note": "ok"}'
        )

        AIClient.complete(
            system_prompt="Test.",
            trusted=None,
            untrusted={"email_body": "Reply to client-77@aliasdomain.example arrived"},
            known_pii={"aliases": ["client-77@aliasdomain.example"]},
            response_schema=_DummyReply,
            call_site="test",
        )

        user_content = instance.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "client-77@aliasdomain.example" not in user_content
        assert "<ALIAS_" in user_content
