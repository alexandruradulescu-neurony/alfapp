"""Fetching the customer (Oblio) invoice to attach to a PayPal first response:
link-on-the-order first, Oblio API fallback, and how it rides _build_submission_files."""

from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.test import TestCase

from apps.claims.models import Claim
from apps.payments import invoice_service as inv
from apps.payments import paypal_disputes_service as pds
from apps.payments.models import Dispute, DisputeSubmission


def _claim(woo='37874'):
    return Claim.objects.create(client_email='b@e.com', woocommerce_id=woo)


def _dispute(claim):
    return Dispute.objects.create(
        paypal_dispute_id='PP-INV', buyer_email='b@e.com', transaction_id='TX',
        transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc), dispute_reason='UNAUTHORISED',
        status='MATCHED', raw_webhook_payload={}, claim=claim)


class InvoiceFetchTests(TestCase):
    def test_no_order_id_returns_none(self):
        self.assertIsNone(inv.fetch_invoice_pdf_for_claim(_claim(woo='')))

    def test_link_on_order_is_used_first(self):
        with patch.object(inv, 'get_woocommerce_order_meta',
                          return_value={'oblio_invoice_link': 'https://oblio.eu/show/x.pdf'}), \
             patch.object(inv, '_download_pdf', return_value=b'%PDF-1.4 invoice') as dl, \
             patch.object(inv, '_oblio_api_link') as api:
            f = inv.fetch_invoice_pdf_for_claim(_claim())
        self.assertIsNotNone(f)
        self.assertEqual(f['content_type'], 'application/pdf')
        self.assertTrue(f['filename'].endswith('.pdf'))
        api.assert_not_called()                       # link worked → no API call
        dl.assert_called_once()

    def test_api_fallback_when_link_missing(self):
        # No link on the order → use series+number via the Oblio API.
        with patch.object(inv, 'get_woocommerce_order_meta',
                          return_value={'oblio_invoice_series_name': 'ALF',
                                        'oblio_invoice_number': '14556'}), \
             patch.object(inv, '_oblio_api_link', return_value='https://oblio.eu/api.pdf') as api, \
             patch.object(inv, '_download_pdf', side_effect=[None, b'%PDF-1.7 api']):
            f = inv.fetch_invoice_pdf_for_claim(_claim())
        self.assertIsNotNone(f)
        self.assertEqual(f['content_type'], 'application/pdf')
        api.assert_called_once_with('ALF', '14556')

    def test_non_pdf_is_rejected(self):
        with patch.object(inv, 'get_woocommerce_order_meta',
                          return_value={'oblio_invoice_link': 'https://oblio.eu/x'}), \
             patch.object(inv, '_oblio_api_link', return_value=None), \
             patch.object(inv, '_download_pdf', return_value=b'<html>login page</html>'):
            self.assertIsNone(inv.fetch_invoice_pdf_for_claim(_claim()))


class BuildFilesInvoiceTests(TestCase):
    def _sub(self, **kw):
        d = _dispute(_claim())
        base = dict(dispute=d, notes='n', attach_evidence_pdf=False, attach_terms=False)
        base.update(kw)
        return DisputeSubmission.objects.create(**base)

    def test_invoice_attached_when_ticked(self):
        sub = self._sub(attach_invoice=True)
        fake = {'name': 'invoice_37874.pdf', 'filename': 'invoice_37874.pdf',
                'content': b'%PDF', 'content_type': 'application/pdf'}
        with patch('apps.payments.invoice_service.fetch_invoice_pdf_for_claim', return_value=fake):
            files = pds._build_submission_files(sub)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['filename'], 'invoice_37874.pdf')

    def test_invoice_not_fetched_when_unticked(self):
        sub = self._sub(attach_invoice=False)
        with patch('apps.payments.invoice_service.fetch_invoice_pdf_for_claim') as fetch:
            files = pds._build_submission_files(sub)
        fetch.assert_not_called()
        self.assertEqual(files, [])

    def test_invoice_unavailable_is_safe(self):
        sub = self._sub(attach_invoice=True)
        with patch('apps.payments.invoice_service.fetch_invoice_pdf_for_claim', return_value=None):
            self.assertEqual(pds._build_submission_files(sub), [])   # no crash, nothing attached
