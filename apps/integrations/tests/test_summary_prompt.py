"""Guard: the claim-summary prompt must keep prioritising the blocker (client
responsiveness) and trusting the latest verification — the behaviour the
summary engine was tuned for (see ticket ALF6420784 feedback)."""

from apps.integrations.briefing import SUMMARY_PROMPT


def test_summary_prompt_leads_with_blocker_and_client_responsiveness():
    p = SUMMARY_PROMPT.lower()
    assert 'blocking' in p
    assert 'client' in p and 'respon' in p  # client responsiveness / not replied
    assert 'stalled' in p


def test_summary_prompt_trusts_latest_verification():
    p = SUMMARY_PROMPT.lower()
    assert 'most recent' in p
    assert 'conflict' in p  # instructs NOT to present a superseded check as a conflict
