"""Wave 1 refund-pipeline hardening (2026-06-12).

Covers: robust inbound claim matching, currency + partial handling,
idempotent Zendesk side-effects, PayPal-webhook auth, and the locked-down
refund API verbs.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.payments.models import Refund
from apps.payments.refund_service import RefundService
from apps.config.models import SystemSettings

User = get_user_model()
SECRET = 'refund-wave1-secret'


def _claim(**kw):
    base = dict(client_email='c@example.com', zd_ticket_id='70123',
                alf_claim_id='ALF7012300', price_paid=Decimal('89.00'))
    base.update(kw)
    return Claim.objects.create(**base)


# ---- inbound recorder ----

class ProcessWooCommerceRefundTests(TestCase):
    def setUp(self):
        self.claim = _claim()
        self.svc = RefundService()

    def test_matches_by_alf_claim_id(self):
        r = self.svc.process_woocommerce_refund(
            claim_number='ALF7012300', refund_amount=Decimal('89.00'),
            refund_id='1001', order_id='555')
        self.assertTrue(r['success'])
        self.assertEqual(r['refund'].claim_id, self.claim.id)

    def test_matches_by_internal_id_fallback(self):
        r = self.svc.process_woocommerce_refund(
            claim_number=str(self.claim.id), refund_amount=Decimal('89.00'),
            refund_id='1002', order_id='555')
        self.assertTrue(r['success'])
        self.assertEqual(r['refund'].claim_id, self.claim.id)

    def test_unknown_claim_reported_not_crashed(self):
        r = self.svc.process_woocommerce_refund(
            claim_number='ALF9999999', refund_amount=Decimal('10.00'),
            refund_id='1003', order_id='555')
        self.assertFalse(r['success'])
        self.assertIn('not found', r['error'])

    def test_currency_from_payload_preserved(self):
        r = self.svc.process_woocommerce_refund(
            claim_number='ALF7012300', refund_amount=Decimal('89.00'),
            refund_id='1004', order_id='555', currency='eur')
        self.assertEqual(r['refund'].currency, 'EUR')

    def test_partial_inferred_from_amount_vs_price_paid(self):
        r = self.svc.process_woocommerce_refund(
            claim_number='ALF7012300', refund_amount=Decimal('20.00'),
            refund_id='1005', order_id='555')
        self.assertEqual(r['refund'].refund_type, 'PARTIAL')

    def test_full_when_amount_equals_price_paid(self):
        r = self.svc.process_woocommerce_refund(
            claim_number='ALF7012300', refund_amount=Decimal('89.00'),
            refund_id='1006', order_id='555')
        self.assertEqual(r['refund'].refund_type, 'FULL')

    def test_explicit_refund_type_overrides_inference(self):
        r = self.svc.process_woocommerce_refund(
            claim_number='ALF7012300', refund_amount=Decimal('20.00'),
            refund_id='1007', order_id='555', refund_type='FULL')
        self.assertEqual(r['refund'].refund_type, 'FULL')

    def test_idempotent_replay_flags_already_processed(self):
        first = self.svc.process_woocommerce_refund(
            claim_number='ALF7012300', refund_amount=Decimal('89.00'),
            refund_id='1008', order_id='555')
        again = self.svc.process_woocommerce_refund(
            claim_number='ALF7012300', refund_amount=Decimal('89.00'),
            refund_id='1008', order_id='555')
        self.assertFalse(first.get('already_processed'))
        self.assertTrue(again.get('already_processed'))
        self.assertEqual(Refund.objects.filter(paypal_refund_id='WC-1008').count(), 1)

    def test_concurrent_create_race_returns_existing_idempotently(self):
        """M1: when two deliveries race and both pass the existence check, the
        second create() hits the unique constraint — it must adopt the winning
        row and report idempotent success, not surface a generic error/500."""
        from unittest.mock import MagicMock
        from django.db import IntegrityError
        winner = MagicMock(id=999)
        with patch('apps.payments.refund_service.Refund') as MockRefund:
            # existence check -> miss, then re-fetch after the race -> winner
            MockRefund.objects.filter.return_value.first.side_effect = [None, winner]
            # reservation reconcile query (filter().order_by().first()) -> miss
            MockRefund.objects.filter.return_value.order_by.return_value.first.return_value = None
            MockRefund.objects.create.side_effect = IntegrityError('duplicate key')
            result = self.svc.process_woocommerce_refund(
                claim_number='ALF7012300', refund_amount=Decimal('89.00'),
                refund_id='RACE1', order_id='555')
        self.assertTrue(result['success'])
        self.assertTrue(result['already_processed'])
        self.assertIs(result['refund'], winner)


# ---- inbound webhook view ----

class RefundWebhookViewTests(TestCase):
    URL = '/api/integrations/zd/refund-webhook/'

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.sidebar_secret_token = SECRET
        ss.save()
        self.claim = _claim(zd_ticket_id='70999', alf_claim_id='ALF7099900')
        self.api = APIClient()
        self.auth = {'HTTP_X_WEBHOOK_SECRET': SECRET}

    def _payload(self, **kw):
        base = {'claim_number': 'ALF7099900', 'refund_id': '2001',
                'refund_amount': '89.00', 'currency': 'USD', 'zd_ticket_id': '70999'}
        base.update(kw)
        return base

    def test_missing_currency_does_not_500(self):
        body = self._payload()
        del body['currency']
        with patch('apps.integrations.views.webhooks.tag_zendesk_ticket_as_refunded') as tag, \
             patch('apps.integrations.views.webhooks.add_refund_comment_to_zendesk'):
            resp = self.api.post(self.URL, body, format='json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        tag.assert_called_once()

    def test_side_effects_skip_on_replay(self):
        with patch('apps.integrations.views.webhooks.tag_zendesk_ticket_as_refunded') as tag, \
             patch('apps.integrations.views.webhooks.add_refund_comment_to_zendesk') as note:
            self.api.post(self.URL, self._payload(), format='json', **self.auth)
            self.api.post(self.URL, self._payload(), format='json', **self.auth)
        self.assertEqual(tag.call_count, 1)   # not re-fired on the retry
        self.assertEqual(note.call_count, 1)

    def test_tags_claims_own_ticket_not_payload(self):
        with patch('apps.integrations.views.webhooks.tag_zendesk_ticket_as_refunded') as tag, \
             patch('apps.integrations.views.webhooks.add_refund_comment_to_zendesk'):
            self.api.post(self.URL, self._payload(zd_ticket_id='99999'),
                          format='json', **self.auth)
        tag.assert_called_once_with('70999')  # claim.zd_ticket_id wins


# ---- PayPal webhook auth ----

class PayPalWebhookAuthTests(TestCase):
    URL = '/api/payments/paypal/webhook/'

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.sidebar_secret_token = SECRET
        ss.save()
        self.api = APIClient()

    def test_anonymous_forgery_rejected(self):
        resp = self.api.post(self.URL, {
            'event_type': 'PAYMENT.CAPTURE.REFUNDED',
            'resource': {'id': 'forged', 'status': 'COMPLETED'},
        }, format='json')
        self.assertEqual(resp.status_code, 401)

    def test_accepted_with_secret(self):
        with patch.object(RefundService, 'process_webhook_refund',
                          return_value={'success': True}):
            resp = self.api.post(self.URL, {'event_type': 'OTHER'},
                                 format='json', HTTP_X_WEBHOOK_SECRET=SECRET)
        self.assertEqual(resp.status_code, 200)


# ---- locked-down refund API ----

class RefundViewSetVerbLockTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username='wave1_mgr', password='x')
        self.api = APIClient()
        self.api.force_authenticate(self.manager)
        self.claim = _claim()
        self.refund = Refund.objects.create(
            claim=self.claim, paypal_refund_id='WC-LOCK1',
            amount=Decimal('89.00'), currency='USD', status='COMPLETED',
            refund_type='FULL', external_source='WOOCOMMERCE')
        self.url = f'/api/payments/refunds/{self.refund.id}/'

    def test_patch_amount_blocked(self):
        resp = self.api.patch(self.url, {'amount': '999.00'}, format='json')
        self.assertEqual(resp.status_code, 405)
        self.refund.refresh_from_db()
        self.assertEqual(self.refund.amount, Decimal('89.00'))

    def test_delete_blocked(self):
        resp = self.api.delete(self.url)
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(Refund.objects.filter(id=self.refund.id).exists())

    def test_manual_create_uses_unique_id(self):
        # count()+1 collided after deletions; uuid suffix must be unique.
        # Use a claim with refund headroom — self.claim is already fully refunded
        # (WC-LOCK1 reserves its whole price_paid), which the M2 cap rightly blocks.
        from apps.payments.views import RefundViewSet  # noqa: F401
        roomy = _claim(alf_claim_id='ALF7012399', zd_ticket_id='70124',
                       price_paid=Decimal('1000.00'))
        r1 = self.api.post('/api/payments/refunds/', {
            'claim_id': roomy.id, 'amount': '5.00',
            'refund_type': 'PARTIAL', 'reason': 'x'}, format='json')
        r2 = self.api.post('/api/payments/refunds/', {
            'claim_id': roomy.id, 'amount': '5.00',
            'refund_type': 'PARTIAL', 'reason': 'y'}, format='json')
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r1.data['paypal_refund_id'], r2.data['paypal_refund_id'])

    def test_manual_create_over_cap_rejected(self):
        """M2 (defense-in-depth): the create API rejects an over-refund."""
        resp = self.api.post('/api/payments/refunds/', {
            'claim_id': self.claim.id, 'amount': '5.00',  # claim already fully refunded
            'refund_type': 'PARTIAL', 'reason': 'over'}, format='json')
        self.assertEqual(resp.status_code, 400)


# ---- refund-requested queue ----

class RefundRequestedQueueTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username='wave1_queue_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.manager)

    def test_refund_requested_claims_listed(self):
        _claim(alf_claim_id='ALF7700001', status='Refund Requested',
               status_category='open', client_name='Refund Wanted')
        _claim(alf_claim_id='ALF7700002', status='Investigation initiated',
               status_category='open', zd_ticket_id='70124')
        resp = self.web.get('/manager/refunds/')
        self.assertEqual(resp.status_code, 200)
        ids = [c.alf_claim_id for c in resp.context['refund_requested']]
        self.assertIn('ALF7700001', ids)
        self.assertNotIn('ALF7700002', ids)
        self.assertContains(resp, 'Awaiting refund decision')
