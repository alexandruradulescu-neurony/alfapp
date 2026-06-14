"""Smoke test: every manager-facing page renders (HTTP 200) without template
errors. A safety net for the design-refresh template sweep — most of these
pages have no other test coverage, so a broken {% url %}, tag, or syntax error
would otherwise ship silently."""

from datetime import datetime, timezone as dt_tz
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from apps.claims.models import Claim
from apps.payments.models import Dispute

User = get_user_model()


class ManagerPageSmokeTests(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='smoke_mgr', password='x', role='MANAGER')
        self.web = Client()
        self.web.force_login(self.mgr)
        self.claim = Claim.objects.create(
            client_email='lee@example.com', client_name='Lee Foley', alf_claim_id='ALF1',
            zd_ticket_id='97001', price_paid=Decimal('74.00'))

    def _get_ok(self, url_name, *args):
        resp = self.web.get(reverse(url_name, args=args))
        self.assertEqual(resp.status_code, 200, f"{url_name} returned {resp.status_code}")

    def test_nav_pages_render(self):
        for name in ['manager_dashboard', 'manager_claims', 'agent_emails',
                     'disputes:dispute_list', 'manager_refunds', 'manager_settings',
                     'manager_users']:
            self._get_ok(name)

    def test_claim_detail_renders(self):
        self._get_ok('agent_claim_detail', self.claim.id)

    def test_dispute_detail_renders(self):
        dispute = Dispute.objects.create(
            paypal_dispute_id='PP-SMOKE-1', claim=self.claim, zd_ticket_id='97001',
            buyer_email='lee@example.com', transaction_id='TX',
            transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc), dispute_reason='UNAUTHORISED')
        # dispute_detail may fetch Zendesk; tolerate that by allowing it to no-op.
        from unittest.mock import patch
        with patch('apps.integrations.services.fetch_zendesk_ticket_full', return_value=None), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]):
            self._get_ok('disputes:dispute_detail', dispute.id)
