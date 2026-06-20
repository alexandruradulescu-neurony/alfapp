"""Phase C core — back-and-forth submissions: endpoint auto-pick, the
provide-supporting-info / generic provide-evidence transport, file assembly,
and the submit_dispute_response orchestration (records + re-sync)."""

import json
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase

from apps.claims.models import Claim
from apps.payments import paypal_disputes_service as pds
from apps.payments.models import (Dispute, DisputeDocument, DisputeActivityLog,
                                  DisputeSubmission, DisputeSubmissionImage)

User = get_user_model()


def _dispute(payload=None, **kw):
    base = dict(paypal_dispute_id='PP-D-SUB', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED',
                raw_webhook_payload=payload or {})
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


class SubmitEndpointTests(TestCase):
    def test_terminal_dispute_has_no_endpoint(self):
        d = _dispute(status='RESOLVED_WON', payload={'dispute_state': 'REQUIRED_ACTION'})
        self.assertEqual(d.submit_endpoint, '')

    def test_resolved_payload_has_no_endpoint(self):
        d = _dispute(payload={'status': 'RESOLVED'})
        self.assertEqual(d.submit_endpoint, '')

    def test_under_review_uses_supporting_info(self):
        d = _dispute(payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        self.assertEqual(d.submit_endpoint, 'provide-supporting-info')

    def test_under_review_by_status_uses_supporting_info(self):
        d = _dispute(payload={'status': 'UNDER_REVIEW'})
        self.assertEqual(d.submit_endpoint, 'provide-supporting-info')

    def test_required_action_chargeback_uses_evidence(self):
        d = _dispute(payload={'dispute_state': 'REQUIRED_ACTION'},
                     dispute_life_cycle_stage='CHARGEBACK')
        self.assertEqual(d.submit_endpoint, 'provide-evidence')

    def test_required_action_inquiry_blocked_by_stage_gate(self):
        # INQUIRY is message-only — can_submit_evidence is False, so no evidence
        # endpoint is offered.
        d = _dispute(payload={'dispute_state': 'REQUIRED_ACTION'},
                     dispute_life_cycle_stage='INQUIRY')
        self.assertEqual(d.submit_endpoint, '')


class EvidenceTypeMapTests(TestCase):
    def test_known_reason_and_default(self):
        self.assertEqual(pds.evidence_type_for_reason('MERCHANDISE_OR_SERVICE_NOT_RECEIVED'),
                         'PROOF_OF_FULFILLMENT')
        self.assertEqual(pds.evidence_type_for_reason('SOMETHING_ELSE'), pds.DEFAULT_EVIDENCE_TYPE)
        self.assertEqual(pds.evidence_type_for_reason(''), pds.DEFAULT_EVIDENCE_TYPE)


class TransportTests(TestCase):
    def test_provide_supporting_info_posts_multipart_to_right_url(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            captured['ct'] = request.headers.get('Content-type')
            captured['body'] = request.data
            return _FakeResponse(b'{"status":"ok"}')

        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', side_effect=fake_urlopen):
            ok, resp = pds.provide_supporting_info('PP-D-1', 'please review this', files=None)

        self.assertTrue(ok)
        self.assertEqual(resp, {'status': 'ok'})
        self.assertTrue(captured['url'].endswith('/v1/customer/disputes/PP-D-1/provide-supporting-info'))
        self.assertIn('multipart/form-data; boundary=', captured['ct'])
        # The JSON input part carries the notes (no evidences array for supporting-info)
        self.assertIn(b'"notes": "please review this"', captured['body'])
        self.assertNotIn(b'evidences', captured['body'])

    def test_provide_evidence_files_wraps_notes_in_evidences(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['url'] = request.full_url
            captured['body'] = request.data
            return _FakeResponse(b'{}')

        files = [{'name': 'a.pdf', 'filename': 'a.pdf', 'content': b'PDF', 'content_type': 'application/pdf'}]
        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', side_effect=fake_urlopen):
            ok, resp = pds.provide_evidence_files('PP-D-2', 'our case', files, evidence_type='PROOF_OF_FULFILLMENT')

        self.assertTrue(ok)
        self.assertTrue(captured['url'].endswith('/PP-D-2/provide-evidence'))
        body = captured['body']
        self.assertIn(b'"evidence_type": "PROOF_OF_FULFILLMENT"', body)
        self.assertIn(b'"document_ids": ["a.pdf"]', body)
        self.assertIn(b'PDF', body)  # the file part is in the multipart body

    def test_http_error_returns_structured_failure(self):
        import urllib.error
        import io

        def boom(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 400, 'Bad Request', {},
                                         io.BytesIO(b'{"name":"INVALID"}'))

        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', side_effect=boom):
            ok, resp = pds.provide_supporting_info('PP-D-3', 'x')
        self.assertFalse(ok)
        self.assertEqual(resp['error'], 'http_error')
        self.assertEqual(resp['code'], 400)

    def test_no_token_fails_without_calling_network(self):
        with patch.object(pds, 'get_paypal_access_token', return_value=None), \
             patch('urllib.request.urlopen') as net:
            ok, resp = pds.provide_supporting_info('PP-D-4', 'x')
            net.assert_not_called()
        self.assertFalse(ok)
        self.assertEqual(resp['error'], 'no_access_token')


class BuildFilesTests(TestCase):
    def test_collects_evidence_pdf_when_ticked_and_images(self):
        d = _dispute()
        doc = DisputeDocument.objects.create(dispute=d, doc_type='EVIDENCE_REPORT',
                                             status='DRAFT', generated_by='MANUAL', version=1)
        doc.file_path.save('report.pdf', ContentFile(b'%PDF-1.4'), save=True)
        sub = DisputeSubmission.objects.create(dispute=d, notes='n', attach_evidence_pdf=True)
        img = DisputeSubmissionImage.objects.create(submission=sub)
        img.file.save('shot.png', ContentFile(b'PNGDATA'), save=True)

        files = pds._build_submission_files(sub)
        # Django suffixes a filename when one already exists on disk, so assert on
        # count + content types + extensions rather than exact names.
        self.assertEqual(len(files), 2)
        self.assertEqual({f['content_type'] for f in files}, {'application/pdf', 'image/png'})
        self.assertTrue(all(f['content'] for f in files))
        self.assertTrue(any(f['filename'].endswith('.pdf') for f in files))
        self.assertTrue(any(f['filename'].endswith('.png') for f in files))

    def test_pdf_skipped_when_not_ticked(self):
        d = _dispute()
        doc = DisputeDocument.objects.create(dispute=d, doc_type='EVIDENCE_REPORT',
                                             status='DRAFT', generated_by='MANUAL', version=1)
        doc.file_path.save('report.pdf', ContentFile(b'%PDF'), save=True)
        sub = DisputeSubmission.objects.create(dispute=d, notes='n', attach_evidence_pdf=False)
        self.assertEqual(pds._build_submission_files(sub), [])

    def test_uploaded_pdf_attachment_sent_as_pdf(self):
        # A manager-uploaded PDF (stored as a submission "image") must go to
        # PayPal with the application/pdf content type, alongside the report.
        d = _dispute()
        doc = DisputeDocument.objects.create(dispute=d, doc_type='EVIDENCE_REPORT',
                                             status='DRAFT', generated_by='MANUAL', version=1)
        doc.file_path.save('report.pdf', ContentFile(b'%PDF-1.4'), save=True)
        sub = DisputeSubmission.objects.create(dispute=d, notes='n', attach_evidence_pdf=True)
        extra = DisputeSubmissionImage.objects.create(submission=sub)
        extra.file.save('terms.pdf', ContentFile(b'%PDF-1.7 terms'), save=True)

        files = pds._build_submission_files(sub)
        self.assertEqual(len(files), 2)                                   # report + uploaded PDF
        self.assertEqual({f['content_type'] for f in files}, {'application/pdf'})
        self.assertEqual(sum(1 for f in files if f['filename'].endswith('.pdf')), 2)


class SubmitOrchestrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sub_mgr', password='x')

    def test_evidence_path_records_and_resyncs(self):
        d = _dispute(payload={'dispute_state': 'REQUIRED_ACTION'},
                     dispute_life_cycle_stage='CHARGEBACK')
        sub = DisputeSubmission.objects.create(dispute=d, notes='our case', source='AI')
        with patch.object(pds, 'provide_evidence_files', return_value=(True, {'ok': 1})) as ev, \
             patch.object(pds, 'provide_supporting_info') as si, \
             patch.object(pds, 'sync_dispute_from_paypal') as sync:
            result = pds.submit_dispute_response(sub, performed_by=self.user)
            ev.assert_called_once()
            si.assert_not_called()
            sync.assert_called_once_with('PP-D-SUB')
        sub.refresh_from_db()
        self.assertTrue(result)
        self.assertEqual(sub.status, 'SUBMITTED')
        self.assertEqual(sub.kind, 'EVIDENCE')
        self.assertIsNotNone(sub.submitted_at)
        self.assertEqual(sub.submitted_by, self.user)
        self.assertEqual(sub.paypal_response, {'ok': 1})
        self.assertTrue(DisputeActivityLog.objects.filter(dispute=d, action='EVIDENCE_SENT').exists())

    def test_under_review_uses_supporting_info(self):
        d = _dispute(payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        sub = DisputeSubmission.objects.create(dispute=d, notes='extra info', source='MANUAL')
        with patch.object(pds, 'provide_evidence_files') as ev, \
             patch.object(pds, 'provide_supporting_info', return_value=(True, {})) as si, \
             patch.object(pds, 'sync_dispute_from_paypal'):
            result = pds.submit_dispute_response(sub, performed_by=self.user)
            ev.assert_not_called()
            si.assert_called_once()
        sub.refresh_from_db()
        self.assertTrue(result)
        self.assertEqual(sub.kind, 'SUPPORTING_INFO')
        self.assertEqual(sub.status, 'SUBMITTED')

    def test_failure_marks_failed_and_skips_resync(self):
        d = _dispute(payload={'dispute_state': 'REQUIRED_ACTION'},
                     dispute_life_cycle_stage='CHARGEBACK')
        sub = DisputeSubmission.objects.create(dispute=d, notes='our case')
        with patch.object(pds, 'provide_evidence_files',
                          return_value=(False, {'error': 'http_error', 'code': 422})), \
             patch.object(pds, 'sync_dispute_from_paypal') as sync:
            result = pds.submit_dispute_response(sub, performed_by=self.user)
            sync.assert_not_called()
        sub.refresh_from_db()
        self.assertFalse(result)
        self.assertEqual(sub.status, 'FAILED')
        self.assertEqual(sub.paypal_response['code'], 422)
        self.assertIsNone(sub.submitted_at)

    def test_no_endpoint_fails_without_calling_paypal(self):
        d = _dispute(status='RESOLVED_WON', payload={'status': 'RESOLVED'})
        sub = DisputeSubmission.objects.create(dispute=d, notes='x')
        with patch.object(pds, 'provide_evidence_files') as ev, \
             patch.object(pds, 'provide_supporting_info') as si:
            result = pds.submit_dispute_response(sub, performed_by=self.user)
            ev.assert_not_called()
            si.assert_not_called()
        sub.refresh_from_db()
        self.assertFalse(result)
        self.assertEqual(sub.status, 'FAILED')
        self.assertEqual(sub.paypal_response, {'error': 'no_submit_endpoint'})


class ServiceDeclarationTests(TestCase):
    """Every INITIAL response (provide-evidence) must declare the transaction is
    an intangible service, not a product — PayPal has no structured field for it."""

    def _capture_body(self, fn, *args, **kwargs):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured['body'] = request.data
            return _FakeResponse(b'{}')

        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen', side_effect=fake_urlopen):
            fn(*args, **kwargs)
        return captured['body'].decode('utf-8', 'replace')

    def test_helper_prepends_declaration(self):
        out = pds._lead_with_service_declaration('our specific argument')
        self.assertIn(pds.SERVICE_NOT_PRODUCT_MARKER, out.lower())
        self.assertIn('our specific argument', out)            # body preserved
        self.assertTrue(out.startswith(pds.SERVICE_NOT_PRODUCT_DECLARATION))

    def test_helper_is_idempotent(self):
        already = pds.SERVICE_NOT_PRODUCT_DECLARATION + "\n\nmore detail"
        out = pds._lead_with_service_declaration(already)
        self.assertEqual(out.lower().count(pds.SERVICE_NOT_PRODUCT_MARKER), 1)

    def test_helper_handles_empty_notes(self):
        self.assertEqual(pds._lead_with_service_declaration(''),
                         pds.SERVICE_NOT_PRODUCT_DECLARATION)

    def test_initial_response_includes_declaration(self):
        body = self._capture_body(pds.provide_evidence_files, 'PP-D-X', 'our case', [])
        self.assertIn(pds.SERVICE_NOT_PRODUCT_MARKER, body.lower())
        self.assertIn('not a', body.lower())                   # "...not a physical product"
        self.assertIn('our case', body)

    def test_followup_does_not_add_declaration(self):
        # The back-and-forth channel (provide-supporting-info) is NOT the initial
        # request, so it must not inject the declaration.
        body = self._capture_body(pds.provide_supporting_info, 'PP-D-X', 'follow-up note', [])
        self.assertNotIn(pds.SERVICE_NOT_PRODUCT_MARKER, body.lower())
        self.assertIn('follow-up note', body)
