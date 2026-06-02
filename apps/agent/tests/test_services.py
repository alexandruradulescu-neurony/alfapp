"""
Tests for AgentChatService migration to AIClient with role separation.

Task 17: Verify that process_message routes through AIClient.complete() with
ChatAnswer schema, proper system/user role separation, and the defense preamble
in the system message.
"""

import pytest
from unittest.mock import patch, MagicMock


@pytest.mark.django_db
def test_chat_uses_ai_client_with_validated_schema(db):
    """After migration, the chat goes through AIClient with ChatAnswer schema."""
    from apps.agent.services import AgentChatService
    from apps.config.models import SystemSettings
    ss, _ = SystemSettings.objects.get_or_create(pk=1, defaults={
        'ai_api_key': 'test',
        'ai_api_base': 'https://api.example.com/v1',
        'ai_api_model': 'test-model',
        'pii_tokenization_salt': 'test_salt_long_enough_for_real_use',
    })
    ss.ai_api_key = 'test'
    ss.ai_api_base = 'https://api.example.com/v1'
    ss.ai_api_model = 'test-model'
    ss.pii_tokenization_salt = 'test_salt_long_enough_for_real_use'
    ss.save()

    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=
                '{"answer":"Status: Found","sources":["claim"]}'
            ))],
        )

        svc = AgentChatService()
        reply = svc.process_message(
            message="status for ALF1234567?",
            conversation_history=[],
        )

    # reply is a ChatResponse; answer should contain the schema's answer value
    answer_text = reply.answer if hasattr(reply, 'answer') else (reply.get('answer') if isinstance(reply, dict) else str(reply))
    assert "Status: Found" in answer_text, f"Expected 'Status: Found' in answer, got: {answer_text!r}"

    # Defense preamble must be present in the system role message
    sent = MockOpenAI.return_value.chat.completions.create.call_args.kwargs["messages"]
    assert sent[0]["role"] == "system", f"First message should be system, got: {sent[0]['role']!r}"
    assert "SECURITY NOTE" in sent[0]["content"], "Defense preamble missing from system message"


@pytest.mark.django_db
def test_chat_returns_chat_response_on_validation_error(db):
    """When the LLM returns invalid JSON, process_message returns a graceful fallback."""
    from apps.agent.services import AgentChatService, ChatResponse
    from apps.config.models import SystemSettings
    ss, _ = SystemSettings.objects.get_or_create(pk=1, defaults={
        'ai_api_key': 'test',
        'ai_api_base': 'https://api.example.com/v1',
        'ai_api_model': 'test-model',
        'pii_tokenization_salt': 'test_salt_long_enough_for_real_use',
    })
    ss.ai_api_key = 'test'
    ss.ai_api_base = 'https://api.example.com/v1'
    ss.ai_api_model = 'test-model'
    ss.pii_tokenization_salt = 'test_salt_long_enough_for_real_use'
    ss.save()

    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        # Return non-JSON to trigger AIResponseValidationError
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='not valid json at all'))],
        )

        with patch('lora_app.settings.AI_VALIDATION_STRICT', True):
            svc = AgentChatService()
            reply = svc.process_message(
                message="status for ALF1234567?",
                conversation_history=[],
            )

    assert isinstance(reply, ChatResponse)
    # Should be a graceful fallback, not an exception
    assert reply.answer is not None
    assert isinstance(reply.sources, list)
    assert isinstance(reply.claims, list)


@pytest.mark.django_db
def test_chat_system_prompt_contains_instructions(db):
    """The system prompt sent to OpenAI contains the LORA assistant instructions."""
    from apps.agent.services import AgentChatService
    from apps.config.models import SystemSettings
    ss, _ = SystemSettings.objects.get_or_create(pk=1, defaults={
        'ai_api_key': 'test',
        'ai_api_base': 'https://api.example.com/v1',
        'ai_api_model': 'test-model',
        'pii_tokenization_salt': 'test_salt_long_enough_for_real_use',
    })
    ss.ai_api_key = 'test'
    ss.ai_api_base = 'https://api.example.com/v1'
    ss.ai_api_model = 'test-model'
    ss.pii_tokenization_salt = 'test_salt_long_enough_for_real_use'
    ss.save()

    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=
                '{"answer":"OK","sources":[]}'
            ))],
        )

        svc = AgentChatService()
        svc.process_message(message="hello", conversation_history=[])

    sent = MockOpenAI.return_value.chat.completions.create.call_args.kwargs["messages"]
    system_content = sent[0]["content"]
    # The SYSTEM_PROMPT class attribute text should appear in system role
    assert "LORA" in system_content or "assistant" in system_content.lower(), (
        f"Expected LORA instructions in system prompt, got: {system_content[:200]!r}"
    )
    # User message should not be empty (it has at least the agent's question)
    user_content = sent[1]["content"]
    assert "hello" in user_content or len(user_content) > 0
