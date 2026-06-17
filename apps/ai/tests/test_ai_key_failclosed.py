"""RED-phase fail-closed test for the AI API key (strict TDD).

Invariant under test
--------------------
When the encrypted SystemSettings.ai_api_key cannot be decrypted, the
encryption layer returns the DECRYPTION_FAILED sentinel (a NUL-wrapped,
opaque, NON-EMPTY string — see apps/config/encrypted_fields.py).

_build_openai_client() currently does `OpenAI(api_key=ss.ai_api_key, ...)`
with no sentinel check, so a decrypt-failure sentinel is handed to the OpenAI
SDK as a live API key. The system must FAIL CLOSED: refuse to build the client
(raise AIClientError) rather than construct OpenAI(...) with the sentinel key.

This sentinel test is expected to FAIL against current code (the bug builds the
client anyway). The happy-path test proves the eventual guard won't break the
normal path; it must pass now and after the fix.

We never persist the sentinel (the encryption layer refuses), so we MOCK the
read: SystemSettings.get_instance() is patched to return a stub whose ai_api_key
we control. The OpenAI symbol imported into apps.ai.client is patched so we can
assert it was / was not constructed.
"""

import types

import pytest
from unittest.mock import patch

from apps.config.encrypted_fields import DECRYPTION_FAILED
from apps.ai.client import _build_openai_client
from apps.ai.exceptions import AIClientError


def _settings_stub(*, ai_api_key,
                   ai_api_base="https://api.example.com/v1",
                   ai_api_model="qwen-turbo"):
    """A stand-in for SystemSettings.get_instance() with a controllable key."""
    return types.SimpleNamespace(
        ai_api_key=ai_api_key,
        ai_api_base=ai_api_base,
        ai_api_model=ai_api_model,
    )


class TestBuildOpenAIClientFailClosed:

    @patch("apps.ai.client.OpenAI")
    @patch("apps.ai.client.SystemSettings.get_instance")
    def test_sentinel_key_does_not_build_client(self, mock_get_instance, mock_openai):
        """SENTINEL: ai_api_key is the decrypt-failure sentinel -> must raise
        AIClientError and never construct OpenAI(...)."""
        mock_get_instance.return_value = _settings_stub(ai_api_key=DECRYPTION_FAILED)

        with pytest.raises(AIClientError):
            _build_openai_client()

        mock_openai.assert_not_called()

    @patch("apps.ai.client.OpenAI")
    @patch("apps.ai.client.SystemSettings.get_instance")
    def test_sentinel_key_never_passed_to_openai(self, mock_get_instance, mock_openai):
        """SENTINEL: even if (wrongly) constructed, the sentinel must never be the
        api_key. Asserting OpenAI is not called with the sentinel key catches the
        bug from a second angle."""
        mock_get_instance.return_value = _settings_stub(ai_api_key=DECRYPTION_FAILED)

        try:
            _build_openai_client()
        except AIClientError:
            pass

        for call in mock_openai.call_args_list:
            assert call.kwargs.get("api_key") != DECRYPTION_FAILED, (
                "OpenAI client was constructed with the DECRYPTION_FAILED sentinel "
                "as its api_key"
            )

    @patch("apps.ai.client.OpenAI")
    @patch("apps.ai.client.SystemSettings.get_instance")
    def test_normal_key_builds_client(self, mock_get_instance, mock_openai, settings):
        """HAPPY PATH: with a real key, _build_openai_client() constructs the
        OpenAI client with that key. Must pass now and after the fix."""
        settings.AI_TIMEOUT = 17
        settings.AI_MAX_RETRIES = 1
        mock_get_instance.return_value = _settings_stub(ai_api_key="sk-real-key")

        client = _build_openai_client()

        assert client is mock_openai.return_value
        mock_openai.assert_called_once()
        _, kwargs = mock_openai.call_args
        assert kwargs.get("api_key") == "sk-real-key"
