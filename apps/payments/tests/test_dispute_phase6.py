"""Phase 6 polish — AI-HTML sanitization + accept-claim state guard (2026-06-13)."""

from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.payments.models import Dispute
from apps.payments import frontend_views

User = get_user_model()


class SanitizeHtmlTests(TestCase):
    def test_strips_script_and_handlers_keeps_formatting(self):
        dirty = ('<p>Real evidence <strong>here</strong>.</p>'
                 '<script>steal()</script>'
                 '<img src=x onerror="alert(1)">')
        clean = frontend_views.sanitize_document_html(dirty)
        self.assertIn('<strong>here</strong>', clean)
        self.assertNotIn('<script>', clean)
        self.assertNotIn('onerror', clean)


class AcceptClaimGuardTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='ph6_mgr', password='x', role='MANAGER')
        self.web = Client()
        self.web.force_login(self.manager)
        self.dispute = Dispute.objects.create(
            paypal_dispute_id='PP-D-6001', buyer_email='b@example.com',
            transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
            status='RESOLVED_WON')

    def test_accept_refused_on_resolved_dispute(self):
        with patch('apps.payments.frontend_views.accept_claim') as accept:
            resp = self.web.post(f'/manager/disputes/{self.dispute.id}/accept-claim/', follow=True)
        self.assertEqual(resp.status_code, 200)
        accept.assert_not_called()  # never reaches PayPal
        self.assertContains(resp, 'already')
