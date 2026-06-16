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
            username='delete_manager', password='x')
        self.agent = User.objects.create_user(
            username='delete_agent', password='x')
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

    def test_claim_with_refund_refuses_with_clear_message(self):
        Refund.objects.create(
            claim=self.claim, paypal_refund_id='PPR-DEL-1',
            amount='10.00', refund_type='FULL')
        self.api.force_authenticate(self.manager)
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 409)
        self.assertIn('refunds or disputes', resp.data['detail'])
        self.assertTrue(Claim.objects.filter(id=self.claim.id).exists())


class ClaimBulkDeleteTests(TestCase):
    URL = '/api/claims/claims/bulk-delete/'

    def setUp(self):
        self.manager = User.objects.create_user(
            username='bulk_manager', password='x')
        self.agent = User.objects.create_user(
            username='bulk_agent', password='x')
        self.api = APIClient()
        self.junk1 = Claim.objects.create(
            client_email='j1@example.com', zd_ticket_id='92001')
        self.junk2 = Claim.objects.create(
            client_email='j2@example.com', zd_ticket_id='92002')
        self.protected = Claim.objects.create(
            client_email='money@example.com', zd_ticket_id='92003')
        Refund.objects.create(
            claim=self.protected, paypal_refund_id='PPR-BULK-1',
            amount='25.00', refund_type='FULL')

    def test_deletes_selected_and_reports_blocked(self):
        EmailLog.objects.create(
            claim=self.junk1, subject='kept', body='kept', zd_ticket_id='92001')
        self.api.force_authenticate(self.manager)
        resp = self.api.post(
            self.URL,
            {'ids': [self.junk1.id, self.junk2.id, self.protected.id]},
            format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertCountEqual(resp.data['deleted'], [self.junk1.id, self.junk2.id])
        self.assertEqual(resp.data['blocked'], [self.protected.id])
        self.assertFalse(Claim.objects.filter(
            id__in=[self.junk1.id, self.junk2.id]).exists())
        self.assertTrue(Claim.objects.filter(id=self.protected.id).exists())
        # The junk claim's email survives, detached
        self.assertTrue(EmailLog.objects.filter(
            zd_ticket_id='92001', claim__isnull=True).exists())

    def test_rejects_malformed_payload(self):
        self.api.force_authenticate(self.manager)
        for bad in ({}, {'ids': []}, {'ids': 'all'}, {'ids': ['x']}):
            resp = self.api.post(self.URL, bad, format='json')
            self.assertEqual(resp.status_code, 400)

    def test_unknown_ids_are_simply_not_deleted(self):
        self.api.force_authenticate(self.manager)
        resp = self.api.post(self.URL, {'ids': [999999]}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['deleted'], [])
        self.assertEqual(resp.data['blocked'], [])
