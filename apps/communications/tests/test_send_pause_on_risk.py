"""P1b: Block client-update SENDS while a claim is at-risk.

Tests are written blind to the gate implementation (TDD / RED first):
  1. send_initial_update(at_risk_claim) -> falsy; client_report_sent_at stays None; Zendesk NOT called.
  2. send_initial_update(normal_claim)  -> truthy; client_report_sent_at set; Zendesk called.
  3. send_follow_up(at_risk update)    -> falsy; update NOT marked SENT.
  4. run_due_updates with autosend + at-risk claim -> update HELD; send_follow_up NOT called.
  5. View POST to claim_client_report_send for at-risk claim -> client_report_sent_at stays None.
"""

from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase, Client as WebClient
from django.urls import reverse
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications.models import ClientUpdate
from apps.communications import client_updates as cu

User = get_user_model()


def _at_risk_claim(**kw):
    base = dict(client_email='c@example.com', zd_ticket_id='ZD-RISK', alf_claim_id='ALF0001',
                client_name='Risk Client', object_description='blue suitcase')
    base.update(kw)
    claim = Claim.objects.create(**base)
    claim.register_risk(reasons=['refund_demanded'], level='at_risk', detail='wants money back')
    return claim


def _normal_claim(**kw):
    base = dict(client_email='n@example.com', zd_ticket_id='ZD-NORM', alf_claim_id='ALF0002',
                client_name='Normal Client', object_description='red bag')
    base.update(kw)
    return Claim.objects.create(**base)


class SendInitialUpdatePauseTest(TestCase):
    """Tests 1 & 2: send_initial_update respects risk gate."""

    def test_at_risk_returns_falsy_no_send_no_timestamp(self):
        """Gate 1: at-risk claim must NOT send and must NOT set client_report_sent_at."""
        claim = _at_risk_claim()
        self.assertTrue(claim.risk_active)

        with patch('apps.integrations.services.post_zendesk_comment') as mock_post:
            result = cu.send_initial_update(claim, 'Hello, we searched for you.')

        self.assertFalse(result)
        claim.refresh_from_db()
        self.assertIsNone(claim.client_report_sent_at)
        mock_post.assert_not_called()

    def test_normal_claim_sends_and_sets_timestamp(self):
        """Gate 2 guard: normal (non-risk) claim must still send and set the timestamp."""
        claim = _normal_claim()
        self.assertFalse(claim.risk_active)

        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}) as mock_post:
            result = cu.send_initial_update(claim, 'Hello, we searched for you.')

        self.assertTrue(result)
        claim.refresh_from_db()
        self.assertIsNotNone(claim.client_report_sent_at)
        mock_post.assert_called_once()


class SendFollowUpPauseTest(TestCase):
    """Test 3: send_follow_up refuses while claim is at-risk."""

    def setUp(self):
        self.claim = _at_risk_claim()
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))
        self.update = self.claim.follow_up_updates.get(milestone='DAY_2')
        self.update.state = ClientUpdate.STATE_DRAFTED
        self.update.draft_body = 'Still searching for you.'
        self.update.save()

    def test_at_risk_returns_falsy_update_not_sent(self):
        """Gate 3: follow-up for an at-risk claim must not be sent."""
        self.assertTrue(self.update.claim.risk_active)

        with patch('apps.integrations.services.post_zendesk_comment') as mock_post:
            result = cu.send_follow_up(self.update, 'Still searching for you.')

        self.assertFalse(result)
        self.update.refresh_from_db()
        self.assertNotEqual(self.update.state, ClientUpdate.STATE_SENT)
        mock_post.assert_not_called()


class RunDueUpdatesPauseTest(TestCase):
    """Test 4: run_due_updates holds at-risk claims without calling send_follow_up."""

    def setUp(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.client_updates_autosend = True
        ss.save()

        self.claim = _at_risk_claim()
        # Schedule a due update (past due)
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))
        self.update = self.claim.follow_up_updates.get(milestone='DAY_2')

    def test_at_risk_claim_is_held_not_sent(self):
        """Gate 4: run_due_updates must hold (not send) an at-risk claim's due update."""
        with patch.object(cu, 'send_follow_up') as mock_send, \
             patch.object(cu, 'prepare_follow_up', return_value=self.update) as mock_prepare, \
             patch.object(cu, 'object_found', return_value=False):
            result = cu.run_due_updates()

        # held incremented, send_follow_up never called
        self.assertEqual(result['held'], 1)
        mock_send.assert_not_called()

        # Update should NOT be SENT
        self.update.refresh_from_db()
        self.assertNotEqual(self.update.state, ClientUpdate.STATE_SENT)


class ClaimClientReportSendViewPauseTest(TestCase):
    """Test 5: POST to claim_client_report_send refuses when claim is at-risk."""

    def setUp(self):
        self.user = User.objects.create_user(username='view_test_mgr', password='pass')
        self.web = WebClient()
        self.web.force_login(self.user)

    def test_at_risk_claim_view_refuses_send(self):
        """Gate 5: the manual send view must not set client_report_sent_at for at-risk claims."""
        claim = _at_risk_claim()
        self.assertTrue(claim.risk_active)

        with patch('apps.integrations.services.post_zendesk_comment') as mock_post:
            resp = self.web.post(
                reverse('claim_client_report_send', args=[claim.id]),
                {'body': 'Your item is being searched for.'}
            )

        # Redirect happens regardless (user is sent back)
        self.assertIn(resp.status_code, [200, 302])

        claim.refresh_from_db()
        self.assertIsNone(claim.client_report_sent_at)
        mock_post.assert_not_called()
