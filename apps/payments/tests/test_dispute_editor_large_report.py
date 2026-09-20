"""Pins the evidence-report editor's large-save path.

The in-place editor (``disputes:dispute_edit_document``) posts the WHOLE
report back as a single ``content_html`` form field on every save. Reports
embed photos as base64 data URIs, so that field is routinely 2.5-10 MB of
HTML. Django's ``DATA_UPLOAD_MAX_MEMORY_SIZE`` defaults to 2.5 MB and rejects
larger request bodies with an HTTP 400 (``RequestDataTooBig``) the moment the
view touches ``request.POST`` — before a single line of the save logic runs.

This bit production on 2026-09-20: five consecutive saves of a 2.47 MB
evidence report all came back 400, and every one of the manager's edits was
silently dropped (no document was ever written).

These tests pin the expected, fixed behaviour: saving a 4 MB or 12 MB report
must succeed (redirect, content persisted, version bumped), while a small
save — always well under the old ceiling — must keep working exactly as
before. The largest evidence report seen in production is 9.2 MB raw, so the
12 MB case is comfortably above that.
"""

from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from apps.payments.models import Dispute, DisputeDocument

User = get_user_model()


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-LER', buyer_email='b@e.com', transaction_id='TX',
                transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED', raw_webhook_payload={})
    base.update(kw)
    return Dispute.objects.create(**base)


def _report_html(payload_len):
    """A real HTML document shaped like what the WYSIWYG editor posts back:
    a marker paragraph plus a base64-looking data-URI image payload of the
    given length, standing in for an embedded evidence photo. Built with
    cheap string multiplication rather than a real encode — the view only
    cares about the byte size on the wire, not valid image data."""
    payload = ('QUJD' * (payload_len // 4 + 1))[:payload_len]
    return (
        '<!DOCTYPE html><html><body>'
        '<p>LARGE-REPORT-MARKER edited text</p>'
        f'<img src="data:image/png;base64,{payload}">'
        '</body></html>'
    )


class _Base(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='ler_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)


class LargeReportSaveTests(_Base):
    """The editor must be able to save evidence reports well past the old
    2.5 MB DATA_UPLOAD_MAX_MEMORY_SIZE ceiling."""

    def _document(self):
        d = _dispute()
        return DisputeDocument.objects.create(
            dispute=d, doc_type='EVIDENCE_REPORT', status='DRAFT',
            generated_by='AI', content_html='old body', version=1)

    def _save(self, document, payload_len):
        with patch('apps.payments.document_service._render_to_pdf',
                   return_value=b'%PDF-1.4 fake'):
            return self.web.post(
                reverse('disputes:dispute_edit_document', args=[document.id]),
                {'content_html': _report_html(payload_len), 'version_increment': 'on'})

    def test_saving_a_4mb_report_succeeds(self):
        doc = self._document()
        resp = self._save(doc, 4_000_000)
        self.assertEqual(resp.status_code, 302)
        doc.refresh_from_db()
        self.assertIn('LARGE-REPORT-MARKER', doc.content_html)
        self.assertEqual(doc.version, 2)

    def test_saving_a_12mb_report_succeeds(self):
        doc = self._document()
        resp = self._save(doc, 12_000_000)
        self.assertEqual(resp.status_code, 302)
        doc.refresh_from_db()
        self.assertIn('LARGE-REPORT-MARKER', doc.content_html)
        self.assertEqual(doc.version, 2)

    def test_small_report_still_saves(self):
        """Control: a save well under the old ceiling must keep working
        exactly as before, both pre- and post-fix."""
        doc = self._document()
        resp = self._save(doc, 50)
        self.assertEqual(resp.status_code, 302)
        doc.refresh_from_db()
        self.assertIn('LARGE-REPORT-MARKER', doc.content_html)
        self.assertEqual(doc.version, 2)
