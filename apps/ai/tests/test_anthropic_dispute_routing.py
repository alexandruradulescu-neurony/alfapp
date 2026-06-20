"""The dispute zone is resolved with Anthropic/Claude when an Anthropic key is
configured; every other call site stays on the default provider (DeepSeek), and
disputes fall back to it when no Anthropic key is set. The PII trust boundary
must hold on the Claude path exactly as it does on the default path."""

import json
from unittest.mock import patch, MagicMock

import pytest
from pydantic import BaseModel

from apps.ai.client import AIClient, _anthropic_enabled_for


class _Out(BaseModel):
    summary: str


def _anthropic_http(content: str):
    """A fake `requests.post(...)` return for the Anthropic Messages API."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "content": [{"type": "text", "text": content}],
        "usage": {"input_tokens": 11, "output_tokens": 22},
    }
    return resp


def _openai_response(content: str):
    m = MagicMock()
    m.choices = [MagicMock()]
    m.choices[0].message.content = content
    return m


@pytest.fixture
def settings_with_claude(db):
    from apps.config.models import SystemSettings
    ss, _ = SystemSettings.objects.get_or_create(pk=1, defaults={})
    ss.pii_tokenization_salt = 'test_salt_long_enough_for_real_use'
    ss.ai_api_key = 'deepseek-test'
    ss.ai_api_base = 'https://api.deepseek.com/v1'
    ss.ai_api_model = 'deepseek-chat'
    ss.anthropic_api_key = 'sk-ant-test'
    ss.anthropic_api_base = 'https://api.anthropic.com'
    ss.anthropic_model = 'claude-sonnet-4-6'
    ss.save()
    return ss


def test_enabled_only_for_disputes_with_a_key(settings_with_claude):
    ss = settings_with_claude
    assert _anthropic_enabled_for("dispute_narrative_notes", ss) is True
    assert _anthropic_enabled_for("dispute_evidence_narrative", ss) is True
    assert _anthropic_enabled_for("zendesk_briefing", ss) is False   # non-dispute
    ss.anthropic_api_key = ''
    assert _anthropic_enabled_for("dispute_narrative_notes", ss) is False  # no key


@pytest.mark.django_db
def test_dispute_call_routes_to_claude_and_masks_pii(settings_with_claude):
    with patch('apps.ai.client.requests.post',
               return_value=_anthropic_http(json.dumps({"summary": "ok"}))) as mock_post, \
         patch('apps.ai.client.OpenAI') as MockOpenAI:
        result = AIClient.complete(
            system_prompt="Write the dispute narrative. Return JSON: {\"summary\": ...}",
            trusted={"customer": "Dionna Bassham"},
            untrusted={"email_body": "Dionna Bassham called us"},
            known_pii={"names": ["Dionna Bassham"]},
            response_schema=_Out,
            call_site="dispute_narrative_notes",
        )
    assert isinstance(result, _Out)
    MockOpenAI.assert_not_called()                      # default provider untouched
    mock_post.assert_called_once()
    kwargs = mock_post.call_args.kwargs
    # Right endpoint + auth headers
    assert mock_post.call_args.args[0].endswith('/v1/messages')
    assert kwargs['headers']['x-api-key'] == 'sk-ant-test'
    assert kwargs['headers']['anthropic-version'] == '2023-06-01'
    body = kwargs['json']
    assert body['model'] == 'claude-sonnet-4-6'
    assert body['messages'][0]['role'] == 'user'        # Anthropic requires user-first
    assert 'system' in body and body['max_tokens'] > 0
    assert 'temperature' in body                        # Sonnet 4.6 accepts it
    # TRUST BOUNDARY: the real name must never reach the provider.
    assert 'Dionna Bassham' not in json.dumps(body)


@pytest.mark.django_db
def test_non_dispute_uses_default_provider(settings_with_claude):
    with patch('apps.ai.client.requests.post') as mock_post, \
         patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = \
            _openai_response(json.dumps({"summary": "ok"}))
        AIClient.complete(
            system_prompt="Summarize. Return JSON: {\"summary\": ...}",
            untrusted={"email_body": "hello"}, response_schema=_Out, call_site="zendesk_briefing",
        )
    mock_post.assert_not_called()                       # Claude not called
    MockOpenAI.return_value.chat.completions.create.assert_called_once()


@pytest.mark.django_db
def test_dispute_without_key_falls_back_to_default(settings_with_claude):
    settings_with_claude.anthropic_api_key = ''
    settings_with_claude.save()
    with patch('apps.ai.client.requests.post') as mock_post, \
         patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = \
            _openai_response(json.dumps({"summary": "ok"}))
        AIClient.complete(
            system_prompt="Return JSON: {\"summary\": ...}",
            untrusted={"email_body": "y"}, response_schema=_Out, call_site="dispute_narrative_notes",
        )
    mock_post.assert_not_called()
    MockOpenAI.return_value.chat.completions.create.assert_called_once()


@pytest.mark.django_db
def test_opus_omits_temperature(settings_with_claude):
    settings_with_claude.anthropic_model = 'claude-opus-4-8'
    settings_with_claude.save()
    with patch('apps.ai.client.requests.post',
               return_value=_anthropic_http(json.dumps({"summary": "ok"}))) as mock_post, \
         patch('apps.ai.client.OpenAI'):
        AIClient.complete(
            system_prompt="Return JSON: {\"summary\": ...}",
            untrusted={"email_body": "y"}, response_schema=_Out, call_site="dispute_evidence_narrative",
        )
    body = mock_post.call_args.kwargs['json']
    assert body['model'] == 'claude-opus-4-8'
    assert 'temperature' not in body                    # Opus 4.7+ rejects it
