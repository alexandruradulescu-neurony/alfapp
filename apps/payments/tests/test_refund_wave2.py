"""Wave 2 — LORA-initiated refunds via WooCommerce (2026-06-12, option B).

LORA → WooCommerce → PayPal → Zendesk. WooCommerce is the sole executor.
Covers the WooCommerce client, the reserve-then-execute service method
(hard cap, failure/indeterminate handling), webhook reconciliation of the
reservation, and the manager-only endpoint.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.payments.models import Refund
from apps.payments.refund_service import RefundService
from apps.config.models import SystemSettings

User = get_user_model()


def _claim(**kw):
    base = dict(client_email='c@example.com', zd_ticket_id='75001',
                alf_claim_id='ALF7500100', woocommerce_id='5001',
                price_paid=Decimal('100.00'))
    base.update(kw)
    return Claim.objects.create(**base)


def _configure_wc():
    ss = SystemSettings.get_instance()
    ss.woocommerce_store_url = 'https://store.example.com'
    ss.woocommerce_consumer_key = 'ck_test'
    ss.woocommerce_consumer_secret = 'cs_test'
    ss.save()
    return ss


# ---- WooCommerce client ----

class WooCommerceClientTests(TestCase):
    def setUp(self):
        _configure_wc()

    def test_not_configured_raises(self):
        ss = SystemSettings.get_instance()
        ss.woocommerce_store_url = ''
        ss.save()
        from apps.payments.woocommerce_service import (
            WooCommerceNotConfigured, create_woocommerce_refund)
        with self.assertRaises(WooCommerceNotConfigured):
            create_woocommerce_refund('5001', Decimal('10.00'), 'x')

    def test_posts_to_order_refunds_with_api_refund(self):
        from apps.payments import woocommerce_service as wc
        captured = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"id": 9001, "amount": "10.00"}'

        def fake_urlopen(req, timeout=30):
            captured['url'] = req.full_url
            captured['method'] = req.get_method()
            captured['body'] = req.data
            captured['auth'] = req.headers.get('Authorization', '')
            return FakeResp()

        with patch('urllib.request.urlopen', side_effect=fake_urlopen):
            result = wc.create_woocommerce_refund('5001', Decimal('10.00'), 'reason')

        self.assertTrue(result['success'])
        self.assertEqual(result['refund_id'], '9001')
        self.assertTrue(captured['url'].endswith('/wp-json/wc/v3/orders/5001/refunds'))
        self.assertEqual(captured['method'], 'POST')
        self.assertIn(b'"api_refund": true', captured['body'])
        self.assertTrue(captured['auth'].startswith('Basic '))

    def test_http_error_is_definite_failure(self):
        import urllib.error
        from apps.payments import woocommerce_service as wc
        err = urllib.error.HTTPError('u', 400, 'Bad', {}, None)
        err.read = lambda: b'{"message": "Invalid order"}'
        with patch('urllib.request.urlopen', side_effect=err):
            result = wc.create_woocommerce_refund('5001', Decimal('10.00'), 'x')
        self.assertFalse(result['success'])
        self.assertNotIn('indeterminate', result)

    def test_timeout_is_indeterminate(self):
        from apps.payments import woocommerce_service as wc
        with patch('urllib.request.urlopen', side_effect=TimeoutError('slow')):
            result = wc.create_woocommerce_refund('5001', Decimal('10.00'), 'x')
        self.assertFalse(result['success'])
        self.assertTrue(result['indeterminate'])


# ---- issue_woocommerce_refund ----

class IssueWooCommerceRefundTests(TestCase):
    def setUp(self):
        _configure_wc()
        self.user = User.objects.create_user(username='wc_mgr', password='x', role='MANAGER')
        self.claim = _claim()
        self.svc = RefundService()

    def test_no_order_id_blocked(self):
        self.claim.woocommerce_id = ''
        self.claim.save(update_fields=['woocommerce_id'])
        r = self.svc.issue_woocommerce_refund(self.claim, Decimal('10'), 'x', self.user)
        self.assertFalse(r['success'])
        self.assertIn('order id', r['error'])

    def test_amount_over_remaining_blocked_no_call(self):
        with patch('apps.payments.refund_service.create_woocommerce_refund') as call:
            r = self.svc.issue_woocommerce_refund(self.claim, Decimal('150'), 'x', self.user)
        self.assertFalse(r['success'])
        self.assertIn('exceeds', r['error'])
        call.assert_not_called()
        self.assertEqual(Refund.objects.filter(claim=self.claim).count(), 0)

    def test_happy_path_records_completed_with_wc_id(self):
        with patch('apps.payments.refund_service.create_woocommerce_refund',
                   return_value={'success': True, 'refund_id': '9100'}):
            r = self.svc.issue_woocommerce_refund(self.claim, Decimal('100'), 'full', self.user)
        self.assertTrue(r['success'])
        refund = r['refund']
        self.assertEqual(refund.paypal_refund_id, 'WC-9100')
        self.assertEqual(refund.status, 'COMPLETED')
        self.assertEqual(refund.refund_type, 'FULL')
        self.assertEqual(refund.created_by, self.user)

    def test_partial_when_less_than_paid(self):
        with patch('apps.payments.refund_service.create_woocommerce_refund',
                   return_value={'success': True, 'refund_id': '9101'}):
            r = self.svc.issue_woocommerce_refund(self.claim, Decimal('30'), 'partial', self.user)
        self.assertEqual(r['refund'].refund_type, 'PARTIAL')

    def test_definite_failure_marks_failed_frees_cap(self):
        with patch('apps.payments.refund_service.create_woocommerce_refund',
                   return_value={'success': False, 'error': 'declined'}):
            r = self.svc.issue_woocommerce_refund(self.claim, Decimal('100'), 'x', self.user)
        self.assertFalse(r['success'])
        self.assertEqual(r['refund'].status, 'FAILED')
        # A failed refund doesn't reserve — a later attempt is allowed
        with patch('apps.payments.refund_service.create_woocommerce_refund',
                   return_value={'success': True, 'refund_id': '9102'}):
            r2 = self.svc.issue_woocommerce_refund(self.claim, Decimal('100'), 'retry', self.user)
        self.assertTrue(r2['success'])

    def test_indeterminate_leaves_pending_and_blocks_cap(self):
        with patch('apps.payments.refund_service.create_woocommerce_refund',
                   return_value={'success': False, 'error': 'timeout', 'indeterminate': True}):
            r = self.svc.issue_woocommerce_refund(self.claim, Decimal('100'), 'x', self.user)
        self.assertFalse(r['success'])
        self.assertTrue(r['indeterminate'])
        self.assertEqual(r['refund'].status, 'PENDING')
        # The pending reservation still counts → a second full refund is blocked
        with patch('apps.payments.refund_service.create_woocommerce_refund') as call:
            r2 = self.svc.issue_woocommerce_refund(self.claim, Decimal('100'), 'x', self.user)
        self.assertFalse(r2['success'])
        self.assertIn('exceeds', r2['error'])
        call.assert_not_called()

    def test_webhook_reconciles_pending_reservation(self):
        # Issue leaves a PENDING reservation (timeout)…
        with patch('apps.payments.refund_service.create_woocommerce_refund',
                   return_value={'success': False, 'error': 'timeout', 'indeterminate': True}):
            self.svc.issue_woocommerce_refund(self.claim, Decimal('100'), 'x', self.user)
        before = Refund.objects.filter(claim=self.claim).count()
        # …then the cascade's webhook arrives — it must adopt, not duplicate.
        result = self.svc.process_woocommerce_refund(
            claim_number='ALF7500100', refund_amount=Decimal('100'),
            refund_id='9200', order_id='5001')
        self.assertTrue(result['success'])
        self.assertEqual(Refund.objects.filter(claim=self.claim).count(), before)
        self.assertEqual(result['refund'].paypal_refund_id, 'WC-9200')
        self.assertEqual(result['refund'].status, 'COMPLETED')


# ---- endpoint ----

class IssueEndpointTests(TestCase):
    URL = '/api/payments/refunds/issue/'

    def setUp(self):
        _configure_wc()
        self.manager = User.objects.create_user(username='ep_mgr', password='x', role='MANAGER')
        self.agent = User.objects.create_user(username='ep_agent', password='x', role='AGENT')
        self.api = APIClient()
        self.claim = _claim()

    def test_manager_can_issue(self):
        self.api.force_authenticate(self.manager)
        with patch('apps.payments.refund_service.create_woocommerce_refund',
                   return_value={'success': True, 'refund_id': '9300'}):
            resp = self.api.post(self.URL, {
                'claim_id': self.claim.id, 'amount': '100.00',
                'refund_type': 'FULL', 'reason': 'ok'}, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['refund']['paypal_refund_id'], 'WC-9300')

    def test_agent_forbidden(self):
        self.api.force_authenticate(self.agent)
        resp = self.api.post(self.URL, {
            'claim_id': self.claim.id, 'amount': '100.00',
            'refund_type': 'FULL', 'reason': 'ok'}, format='json')
        self.assertIn(resp.status_code, (403, 401))
        self.assertEqual(Refund.objects.count(), 0)

    def test_indeterminate_returns_502(self):
        self.api.force_authenticate(self.manager)
        with patch('apps.payments.refund_service.create_woocommerce_refund',
                   return_value={'success': False, 'error': 'timeout', 'indeterminate': True}):
            resp = self.api.post(self.URL, {
                'claim_id': self.claim.id, 'amount': '100.00',
                'refund_type': 'FULL', 'reason': 'x'}, format='json')
        self.assertEqual(resp.status_code, 502)
        self.assertTrue(resp.data['indeterminate'])
