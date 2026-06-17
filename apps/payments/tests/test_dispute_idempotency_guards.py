"""Regression tests for the 2026-06-17 review idempotency guards on the PayPal
dispute service: accepting a claim or providing evidence must not re-POST to
PayPal (and so must not move money / submit evidence twice) when the dispute is
already in the corresponding terminal state, and the POSTs carry a stable
PayPal-Request-Id for PayPal-side deduplication."""

from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.test import TestCase

from apps.payments import paypal_disputes_service as pds
from apps.payments.models import Dispute


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-D-IDEM', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED',
                raw_webhook_payload={})
    base.update(kw)
    return Dispute.objects.create(**base)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class AcceptClaimIdempotencyTests(TestCase):
    def test_skips_post_when_already_accepted(self):
        """accept_claim on an already-ACCEPTED dispute must not POST again
        (re-accepting issues a second refund)."""
        _dispute(status=Dispute.STATUS_ACCEPTED)
        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen') as net:
            result = pds.accept_claim('PP-D-IDEM')
        self.assertTrue(result)
        net.assert_not_called()

    def test_skips_post_when_already_resolved(self):
        _dispute(status=Dispute.STATUS_RESOLVED_WON)
        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen') as net:
            result = pds.accept_claim('PP-D-IDEM')
        self.assertTrue(result)
        net.assert_not_called()

    def test_first_accept_posts_with_idempotency_key(self):
        """On the happy path it POSTs once, carrying a stable PayPal-Request-Id."""
        _dispute(status=Dispute.STATUS_MATCHED)
        captured = {}

        def fake_urlopen(request, timeout=None):
            # urllib capitalises header keys: 'PayPal-Request-Id' -> 'Paypal-request-id'
            captured['req_id'] = request.headers.get('Paypal-request-id')
            return _FakeResponse(b'{}')

        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', side_effect=fake_urlopen):
            result = pds.accept_claim('PP-D-IDEM', 'note')

        self.assertTrue(result)
        self.assertEqual(captured['req_id'], 'accept-claim-PP-D-IDEM')
        # And local state advanced to ACCEPTED.
        self.assertEqual(Dispute.objects.get(paypal_dispute_id='PP-D-IDEM').status,
                         Dispute.STATUS_ACCEPTED)
