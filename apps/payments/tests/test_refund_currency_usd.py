"""Refunds the business issues are always in US dollars.

The refund-create API must record 'USD' regardless of any currency the caller
supplies — the client cannot choose a currency. This is scoped to what LORA
*issues*; it deliberately does NOT touch the WooCommerce/PayPal webhook recorder,
which truthfully records the currency a real external refund arrived in (see
test_refund_wave1.ProcessWooCommerceRefundTests.test_currency_from_payload_preserved).
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.payments.models import Refund

User = get_user_model()


class RefundCreateAlwaysUsdTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='usd_test', password='x')
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.claim = Claim.objects.create(
            client_email='c@example.com', zd_ticket_id='70200',
            alf_claim_id='ALF7020000', price_paid=Decimal('500.00'))

    def test_manual_create_records_usd_even_if_client_sends_other_currency(self):
        """A caller-supplied currency must be ignored — the refund records USD."""
        resp = self.api.post('/api/payments/refunds/', {
            'claim_id': self.claim.id, 'amount': '5.00', 'currency': 'EUR',
            'refund_type': 'PARTIAL', 'reason': 'x'}, format='json')
        self.assertEqual(resp.status_code, 201)
        refund = Refund.objects.get(claim=self.claim)
        self.assertEqual(refund.currency, 'USD')
