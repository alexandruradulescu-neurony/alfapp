"""
TDD (RED phase written first):

The AI-reply-driven path inside _draft_follow_up / prepare_follow_up must fall
back to the on-brand milestone_message voice (not the dead _no_news_template
generic text) when:
  (a) safe office replies exist but the AI call raises, or
  (b) safe office replies exist but no API key is configured.

The no-safe-replies path already uses milestone_message (via prepare_follow_up);
these tests verify the AI-failure fallback is also wired to the milestone voice.

Additionally we guard that the module no longer exports _no_news_template or
_final_template after the dead-code removal.
"""

from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications.models import ClientUpdate, EmailLog
from apps.communications import client_updates as cu


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_claim(**kw):
    defaults = dict(client_email='fallback@example.com', client_name='Lee',
                    object_description='Laptop', zd_ticket_id='99009')
    defaults.update(kw)
    return Claim.objects.create(**defaults)


def _safe_reply(claim):
    """One OBJECT_FOUND reply (CLIENT_SAFE) attached to the claim."""
    return EmailLog.objects.create(
        claim=claim,
        from_email='lf@airport.gov',
        subject='Match?',
        body='Possible match found.',
        category=EmailLog.CATEGORY_OBJECT_FOUND,
        ai_summary='Possible match found',
    )


# ---------------------------------------------------------------------------
# 1.  _draft_follow_up falls back to fallback_body when AI raises
# ---------------------------------------------------------------------------

class TestDraftFollowUpAIFailureFallback(TestCase):
    """When safe replies exist but AIClient.complete raises, _draft_follow_up
    must return the fallback_body passed in — which in prepare_follow_up is
    the result of milestone_message(claim, update.milestone, ticket_data, period_days).

    The old behaviour was to return _no_news_template(claim), whose distinctive
    phrase is 'still actively following up' — that phrase must be gone.
    """

    def setUp(self):
        from apps.config.models import SystemSettings
        self.ss = SystemSettings.get_instance()
        self.ss.ai_api_key = 'sk-test'
        self.ss.save()

        self.claim = _make_claim()
        self._reply = _safe_reply(self.claim)

    def test_ai_raises_returns_fallback_body(self):
        fallback = 'MILESTONE FALLBACK BODY'
        with patch('apps.ai.client.AIClient.complete', side_effect=Exception('API down')):
            body, has_news = cu._draft_follow_up(
                self.claim, [self._reply], fallback_body=fallback)
        self.assertEqual(body, fallback)

    def test_ai_raises_does_not_return_no_news_generic_text(self):
        fallback = 'MILESTONE FALLBACK BODY'
        with patch('apps.ai.client.AIClient.complete', side_effect=RuntimeError('timeout')):
            body, _ = cu._draft_follow_up(
                self.claim, [self._reply], fallback_body=fallback)
        self.assertNotIn('still actively following up', body)

    def test_ai_raises_has_news_true(self):
        """has_news is True because we had safe replies (news was available)."""
        fallback = 'MILESTONE FALLBACK'
        with patch('apps.ai.client.AIClient.complete', side_effect=Exception('err')):
            _, has_news = cu._draft_follow_up(
                self.claim, [self._reply], fallback_body=fallback)
        self.assertTrue(has_news)

    def test_ai_empty_body_returns_fallback(self):
        """If AI returns a result but body is empty, fallback_body should be used."""
        fallback = 'MILESTONE FALLBACK'
        empty_result = MagicMock()
        empty_result.body = ''
        with patch('apps.ai.client.AIClient.complete', return_value=empty_result):
            body, _ = cu._draft_follow_up(
                self.claim, [self._reply], fallback_body=fallback)
        self.assertEqual(body, fallback)


# ---------------------------------------------------------------------------
# 2.  _draft_follow_up falls back to fallback_body when no API key
# ---------------------------------------------------------------------------

class TestDraftFollowUpNoApiKeyFallback(TestCase):
    """When safe replies exist but ai_api_key is blank, _draft_follow_up must
    return fallback_body, not the old _no_news_template generic text."""

    def setUp(self):
        from apps.config.models import SystemSettings
        self.ss = SystemSettings.get_instance()
        self.ss.ai_api_key = ''
        self.ss.save()

        self.claim = _make_claim()
        self._reply = _safe_reply(self.claim)

    def test_no_api_key_returns_fallback_body(self):
        fallback = 'MILESTONE FALLBACK NO KEY'
        body, has_news = cu._draft_follow_up(
            self.claim, [self._reply], fallback_body=fallback)
        self.assertEqual(body, fallback)

    def test_no_api_key_does_not_return_no_news_generic_text(self):
        fallback = 'MILESTONE FALLBACK NO KEY'
        body, _ = cu._draft_follow_up(
            self.claim, [self._reply], fallback_body=fallback)
        self.assertNotIn('still actively following up', body)

    def test_no_api_key_has_news_true(self):
        """has_news stays True — we have safe replies, just no key to draft them."""
        fallback = 'MILESTONE FALLBACK'
        _, has_news = cu._draft_follow_up(
            self.claim, [self._reply], fallback_body=fallback)
        self.assertTrue(has_news)


# ---------------------------------------------------------------------------
# 3.  prepare_follow_up threads milestone_message as the fallback
# ---------------------------------------------------------------------------

class TestPrepareFollowUpAIFailureFallback(TestCase):
    """prepare_follow_up: when safe replies exist but AI fails, the drafted
    body must contain the milestone_message voice (greeting + 72% disclaimer),
    not the old 'still actively following up' generic phrase.

    We mock fetch_zendesk_ticket (returns minimal ticket data) and patch
    AIClient.complete to raise, then assert the final draft_body matches what
    milestone_message would produce."""

    def setUp(self):
        from apps.config.models import SystemSettings
        self.ss = SystemSettings.get_instance()
        self.ss.ai_api_key = 'sk-test'
        self.ss.save()

        self.claim = _make_claim()
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))
        self.update = self.claim.follow_up_updates.get(milestone='DAY_2')
        _safe_reply(self.claim)  # ensures safe replies exist

    @patch('apps.integrations.services.fetch_zendesk_ticket',
           return_value={'custom_fields': []})
    def test_ai_failure_draft_contains_milestone_greeting(self, mock_fetch):
        with patch('apps.ai.client.AIClient.complete', side_effect=Exception('AI down')):
            cu.prepare_follow_up(self.update, fetch_email=False)
        self.update.refresh_from_db()
        body = self.update.draft_body
        self.assertIn('Dear ', body)

    @patch('apps.integrations.services.fetch_zendesk_ticket',
           return_value={'custom_fields': []})
    def test_ai_failure_draft_contains_72_percent_disclaimer(self, mock_fetch):
        with patch('apps.ai.client.AIClient.complete', side_effect=Exception('AI down')):
            cu.prepare_follow_up(self.update, fetch_email=False)
        self.update.refresh_from_db()
        self.assertIn('72%', self.update.draft_body)

    @patch('apps.integrations.services.fetch_zendesk_ticket',
           return_value={'custom_fields': []})
    def test_ai_failure_draft_does_not_contain_old_generic_phrase(self, mock_fetch):
        with patch('apps.ai.client.AIClient.complete', side_effect=Exception('AI down')):
            cu.prepare_follow_up(self.update, fetch_email=False)
        self.update.refresh_from_db()
        self.assertNotIn('still actively following up', self.update.draft_body)

    @patch('apps.integrations.services.fetch_zendesk_ticket',
           return_value={'custom_fields': []})
    def test_no_api_key_draft_contains_milestone_greeting(self, mock_fetch):
        self.ss.ai_api_key = ''
        self.ss.save()
        cu.prepare_follow_up(self.update, fetch_email=False)
        self.update.refresh_from_db()
        body = self.update.draft_body
        self.assertIn('Dear ', body)

    @patch('apps.integrations.services.fetch_zendesk_ticket',
           return_value={'custom_fields': []})
    def test_no_api_key_draft_contains_72_percent_disclaimer(self, mock_fetch):
        self.ss.ai_api_key = ''
        self.ss.save()
        cu.prepare_follow_up(self.update, fetch_email=False)
        self.update.refresh_from_db()
        self.assertIn('72%', self.update.draft_body)


# ---------------------------------------------------------------------------
# 4.  Dead-code guard — module must NOT define _no_news_template / _final_template
# ---------------------------------------------------------------------------

class TestDeadTemplatesRemoved(TestCase):
    """After the refactor, _no_news_template and _final_template must not
    exist in the client_updates module."""

    def test_no_news_template_deleted(self):
        self.assertFalse(
            hasattr(cu, '_no_news_template'),
            "_no_news_template was not deleted — it is dead code and must be removed.",
        )

    def test_final_template_deleted(self):
        self.assertFalse(
            hasattr(cu, '_final_template'),
            "_final_template was not deleted — it is dead code and must be removed.",
        )
