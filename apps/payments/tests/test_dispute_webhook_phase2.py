"""Phase 2 — inbound PayPal dispute webhook (2026-06-13).

PayPal posts disputes directly; authenticity is proven by PayPal's signature
verification (fail-closed). CUSTOMER.DISPUTE.CREATED → fetch details, create
the Dispute, match to a claim by buyer email, capture the deadline. Idempotent.
All PayPal HTTP is mocked.
"""

from datetime import datetime, timezone as dt_tz
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments.models import Dispute, ProcessedWebhookEvent
from apps.payments import paypal_disputes_service as svc

URL = '/api/payments/paypal/dispute-webhook/'

DISPUTE_DETAILS = {
    'dispute_id': 'PP-D-9001',
    'reason': 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED',
    'status': 'WAITING_FOR_SELLER_RESPONSE',
    'dispute_life_cycle_stage': 'CHARGEBACK',
    'dispute_amount': {'currency_code': 'USD', 'value': '89.00'},
    'seller_response_due_date': '2026-06-20T10:00:00Z',
    'create_time': '2026-06-13T09:00:00Z',
    'disputed_transactions': [{
        'seller_transaction_id': 'TXN-77',
        'create_time': '2026-06-01T12:00:00Z',
        'buyer': {'email': 'buyer@example.com', 'name': 'Bea Buyer'},
    }],
}


def _event(event_type='CUSTOMER.DISPUTE.CREATED', dispute_id='PP-D-9001', event_id='WH-1'):
    return {'id': event_id, 'event_type': event_type, 'resource_type': 'dispute',
            'resource': {'dispute_id': dispute_id}}


class DisputeWebhookTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.paypal_webhook_id = 'WH-CONFIG-1'
        ss.paypal_mode = 'sandbox'
        ss.save()
        self.api = APIClient()
        self.claim = Claim.objects.create(
            client_email='buyer@example.com', zd_ticket_id='96001',
            alf_claim_id='ALF9600100')

    def test_bad_signature_rejected_no_dispute(self):
        with patch.object(svc, 'verify_webhook_signature', return_value=False):
            resp = self.api.post(URL, _event(), format='json')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(Dispute.objects.count(), 0)

    def test_created_event_ingests_and_matches_claim(self):
        with patch.object(svc, 'verify_webhook_signature', return_value=True), \
             patch.object(svc, 'fetch_dispute_details', return_value=DISPUTE_DETAILS):
            resp = self.api.post(URL, _event(), format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['created'])
        d = Dispute.objects.get(paypal_dispute_id='PP-D-9001')
        self.assertEqual(d.claim_id, self.claim.id)
        self.assertEqual(d.status, 'MATCHED')
        self.assertEqual(d.dispute_amount, Decimal('89.00'))
        self.assertEqual(d.dispute_reason, 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
        self.assertEqual(d.transaction_id, 'TXN-77')
        self.assertIsNotNone(d.seller_response_due)
        self.assertEqual(d.zd_ticket_id, '96001')

    def test_unmatched_dispute_stored_unlinked(self):
        details = dict(DISPUTE_DETAILS)
        details['disputed_transactions'] = [{
            'seller_transaction_id': 'TXN-88',
            'buyer': {'email': 'stranger@nowhere.com', 'name': 'X'}}]
        with patch.object(svc, 'verify_webhook_signature', return_value=True), \
             patch.object(svc, 'fetch_dispute_details', return_value=details):
            resp = self.api.post(URL, _event(dispute_id='PP-D-9002', event_id='WH-2'),
                                 format='json')
        self.assertEqual(resp.status_code, 200)
        d = Dispute.objects.get(paypal_dispute_id='PP-D-9002')
        self.assertIsNone(d.claim_id)
        self.assertEqual(d.status, 'RECEIVED')

    def test_duplicate_event_processed_once(self):
        with patch.object(svc, 'verify_webhook_signature', return_value=True), \
             patch.object(svc, 'fetch_dispute_details', return_value=DISPUTE_DETAILS) as fetch:
            self.api.post(URL, _event(), format='json')
            self.api.post(URL, _event(), format='json')  # same event id
        self.assertEqual(Dispute.objects.filter(paypal_dispute_id='PP-D-9001').count(), 1)
        self.assertEqual(ProcessedWebhookEvent.objects.filter(event_id='WH-1').count(), 1)
        fetch.assert_called_once()  # second delivery short-circuited

    def test_fetch_failure_returns_503_for_retry(self):
        with patch.object(svc, 'verify_webhook_signature', return_value=True), \
             patch.object(svc, 'fetch_dispute_details', return_value=None):
            resp = self.api.post(URL, _event(event_id='WH-3'), format='json')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(Dispute.objects.count(), 0)

    def test_updated_event_acknowledged(self):
        with patch.object(svc, 'verify_webhook_signature', return_value=True):
            resp = self.api.post(URL, _event(event_type='CUSTOMER.DISPUTE.UPDATED',
                                             event_id='WH-4'), format='json')
        self.assertEqual(resp.status_code, 200)


class VerifyWebhookSignatureTests(TestCase):
    def test_no_webhook_id_fails_closed(self):
        ss = SystemSettings.get_instance()
        ss.paypal_webhook_id = ''
        ss.save()
        self.assertFalse(svc.verify_webhook_signature({}, {'id': 'x'}))

    def test_success_verdict_passes(self):
        from unittest.mock import MagicMock
        ss = SystemSettings.get_instance()
        ss.paypal_webhook_id = 'WH-CONFIG-1'
        ss.save()
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b'{"verification_status": "SUCCESS"}'
        with patch.object(svc, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', return_value=resp):
            ok = svc.verify_webhook_signature(
                {'Paypal-Transmission-Id': 't', 'Paypal-Transmission-Sig': 's',
                 'Paypal-Cert-Url': 'u', 'Paypal-Auth-Algo': 'a',
                 'Paypal-Transmission-Time': 'now'},
                {'id': 'WH-1'})
        self.assertTrue(ok)
