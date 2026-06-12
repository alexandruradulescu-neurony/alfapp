"""Tests for manual claim deletion (manager-only junk-ticket cleanup)."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.claims.models import Claim, ClaimUpdateTimeline
from apps.communications.models import EmailLog
from apps.payments.models import Refund

User = get_user_model()


class ClaimDeleteTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username='delete_manager', password='x', role='MANAGER')
        self.agent = User.objects.create_user(
            username='delete_agent', password='x', role='AGENT')
        self.api = APIClient()
        self.claim = Claim.objects.create(
            client_email='junk@example.com', zd_ticket_id='91001',
            alf_claim_id='ALF0091001')
        self.url = f'/api/claims/claims/{self.claim.id}/'

    def test_manager_deletes_claim_with_cascading_records(self):
        ClaimUpdateTimeline.objects.create(
            claim=self.claim, zendesk_ticket_id='91001',
            update_type='STATUS_CHANGE', changes_summary='{}')
        self.api.force_authenticate(self.manager)
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(Claim.objects.filter(id=self.claim.id).exists())
        self.assertFalse(ClaimUpdateTimeline.objects.filter(
            zendesk_ticket_id='91001').exists())

    def test_emails_survive_deletion_detached(self):
        email = EmailLog.objects.create(
            claim=self.claim, subject='kept', body='kept',
            zd_ticket_id='91001')
        self.api.force_authenticate(self.manager)
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 204)
        email.refresh_from_db()
        self.assertIsNone(email.claim_id)  # audit row kept, link cleared

    def test_agent_cannot_delete(self):
        self.api.force_authenticate(self.agent)
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Claim.objects.filter(id=self.claim.id).exists())

    def test_claim_with_refund_refuses_with_clear_message(self):
        Refund.objects.create(
            claim=self.claim, paypal_refund_id='PPR-DEL-1',
            amount='10.00', refund_type='FULL')
        self.api.force_authenticate(self.manager)
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('refunds or disputes', resp.data['detail'])
        self.assertTrue(Claim.objects.filter(id=self.claim.id).exists())
