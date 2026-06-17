"""Regression test for the 2026-06-17 review fix: the client's name must be
registered with the PII tokenizer (known_pii["names"]) before a chat prompt
leaves the LLM trust boundary. The tokenizer cannot detect unknown names, so a
name that is not registered would reach the provider in cleartext."""

import pytest
from unittest.mock import patch, MagicMock

from apps.agent.services import AgentChatService
from apps.claims.models import Claim


@pytest.mark.django_db
def test_client_name_is_registered_as_known_pii():
    Claim.objects.create(
        alf_claim_id="ALF9000001",
        client_email="jdoe@example.com",
        client_name="John Q Public",
    )

    captured = {}

    def fake_complete(**kwargs):
        captured.update(kwargs)
        result = MagicMock()
        result.answer = "ok"
        result.sources = []
        return result

    # Replace the whole LLM call; we only care what known_pii it was handed.
    with patch("apps.ai.client.AIClient.complete", side_effect=fake_complete):
        AgentChatService().process_message("What is the status of ALF9000001?")

    known_pii = captured.get("known_pii") or {}
    assert "John Q Public" in known_pii.get("names", []), known_pii
    # The email alias behaviour is unchanged.
    assert "jdoe@example.com" in known_pii.get("aliases", []), known_pii
