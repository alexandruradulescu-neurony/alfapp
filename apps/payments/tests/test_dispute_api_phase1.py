"""Phase 1 of the dispute pipeline — fix the outgoing PayPal calls (2026-06-13).

Correct paths (/v1/customer/disputes/, slash), multipart evidence upload with
an evidence_type (not base64-in-JSON), /send-message, and a sandbox/live
switch that DEFAULTS TO SANDBOX so no dispute action hits live money until
explicitly flipped. HTTP is always mocked — no real PayPal calls.
"""

import json
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch, MagicMock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments.models import Dispute, DisputeDocument
from apps.payments import paypal_disputes_service as svc


class PaypalApiBaseTests(TestCase):
    def test_defaults_to_sandbox(self):
        ss = SystemSettings.get_instance()
        ss.paypal_mode = ''
        ss.save()
        self.assertIn('sandbox', svc.paypal_api_base())

    def test_live_when_set(self):
        ss = SystemSettings.get_instance()
        ss.paypal_mode = 'live'
        ss.save()
        self.assertEqual(svc.paypal_api_base(), 'https://api-m.paypal.com')


class MultipartEncoderTests(TestCase):
    def test_includes_input_json_and_file_part(self):
        body, content_type = svc._encode_multipart(
            {'evidences': [{'evidence_type': 'PROOF_OF_FULFILLMENT', 'notes': 'hi'}]},
            [{'name': 'doc.pdf', 'filename': 'doc.pdf', 'content': b'%PDF-1.4 data',
              'content_type': 'application/pdf'}])
        self.assertTrue(content_type.startswith('multipart/form-data; boundary='))
        self.assertIn(b'name="input"', body)
        self.assertIn(b'PROOF_OF_FULFILLMENT', body)
        self.assertIn(b'filename="doc.pdf"', body)
        self.assertIn(b'%PDF-1.4 data', body)


def _fake_response(payload):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.read.return_value = json.dumps(payload).encode('utf-8')
    return resp


class DisputeEndpointPathTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.paypal_mode = 'sandbox'
        ss.save()
        self.claim = Claim.objects.create(client_email='c@example.com', zd_ticket_id='95001')
        self.dispute = Dispute.objects.create(
            paypal_dispute_id='PP-D-1', claim=self.claim, buyer_email='b@example.com',
            transaction_id='TX1', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc))

    def test_fetch_uses_slash_path_and_sandbox_host(self):
        with patch.object(svc, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', return_value=_fake_response({'dispute_id': 'PP-D-1'})) as op:
            svc.fetch_dispute_details('PP-D-1')
        url = op.call_args[0][0].full_url
        self.assertEqual(url, 'https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-D-1')

    def test_accept_claim_uses_slash_path(self):
        with patch.object(svc, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', return_value=_fake_response({'status': 'ok'})) as op:
            svc.accept_claim('PP-D-1', note='conceding')
        self.assertTrue(op.call_args[0][0].full_url.endswith('/v1/customer/disputes/PP-D-1/accept-claim'))

    def test_send_message_uses_send_message_path(self):
        with patch.object(svc, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', return_value=_fake_response({'status': 'ok'})) as op:
            svc.send_message('PP-D-1', 'hello buyer')
        self.assertTrue(op.call_args[0][0].full_url.endswith('/v1/customer/disputes/PP-D-1/send-message'))

    def test_provide_evidence_sends_multipart_with_type(self):
        doc = DisputeDocument.objects.create(
            dispute=self.dispute, doc_type='EVIDENCE_REPORT',
            file_path=SimpleUploadedFile('evidence.pdf', b'%PDF-1.4 proof', content_type='application/pdf'))
        with patch.object(svc, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', return_value=_fake_response({'status': 'ok'})) as op:
            ok = svc.provide_evidence('PP-D-1', [doc], 'our evidence', evidence_type='PROOF_OF_FULFILLMENT')
        self.assertTrue(ok)
        req = op.call_args[0][0]
        self.assertTrue(req.full_url.endswith('/v1/customer/disputes/PP-D-1/provide-evidence'))
        self.assertTrue(req.headers['Content-type'].startswith('multipart/form-data'))
        self.assertIn(b'PROOF_OF_FULFILLMENT', req.data)
        self.assertIn(b'%PDF-1.4 proof', req.data)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.status, 'EVIDENCE_SENT')
