"""Red-phase spec — the evidence-report PDF must reliably reach PayPal.

Managers kept sending dispute replies WITHOUT the generated evidence report:
the "Attach the evidence-report PDF" tick was easy to miss, and screen state
was only persisted on the explicit save action. This file pins the NEW
contract (deliberately unimplemented — these tests must fail until it lands):

1. Composer checkboxes are screen truth on EVERY action: POSTing
   action='generate' also persists the three attach_* flags from the POST
   onto the working draft (checkbox semantics: 'on' → True, omitted → False).
2. Generating the evidence report auto-ticks attach_evidence_pdf on the
   existing working DRAFT submission.
3. action='send' on the prepare-submission form is a one-step save+submit:
   the draft is saved from the POST and submitted to PayPal in the same
   request. Sending WITHOUT the report is allowed (no server-side block);
   blank notes must send nothing.
4. The legacy two-step flow (saved DRAFT → POST submit-to-paypal) still works.
5. The EVIDENCE_SENT activity-log entry records WHAT was attached — the
   attached file name(s), or an explicit "no attachments".
6. On the detail page, with no working draft, the report tick DEFAULTS ON
   when a report exists and was never part of a SUBMITTED submission; the
   context exposes this as `report_already_sent`.
7. The literal confirmation copy "Send without the report?" renders only
   while a report exists that has never been sent (first-time-only warning).

Transport is mocked at apps.payments.paypal_disputes_service
._post_dispute_action_multipart — nothing here talks to PayPal.
"""

import re
from contextlib import contextmanager
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.payments.models import (Dispute, DisputeActivityLog, DisputeDocument,
                                  DisputeSubmission)

User = get_user_model()

GENERATE_AI_RESULT = {'notes': 'drafted text', 'source': 'AI'}
SEND_WITHOUT_REPORT_COPY = 'Send without the report?'

_ATTACH_PDF_INPUT_RE = re.compile(
    r'<input[^>]*name=["\']attach_evidence_pdf["\'][^>]*>')


def _dispute(payload=None, **kw):
    base = dict(paypal_dispute_id='PP-D-RPT', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED', status='MATCHED',
                raw_webhook_payload=payload or {})
    base.update(kw)
    return Dispute.objects.create(**base)


def _evidence_open_dispute(**kw):
    """A dispute PayPal accepts an evidence submission for
    (dispute.submit_endpoint == 'provide-evidence')."""
    return _dispute(payload={'dispute_state': 'REQUIRED_ACTION'},
                    dispute_life_cycle_stage='CHARGEBACK', **kw)


def _report_doc(dispute, filename='report.pdf'):
    """A generated EVIDENCE_REPORT document with a real file on disk."""
    doc = DisputeDocument.objects.create(
        dispute=dispute, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
        status=DisputeDocument.STATUS_DRAFT, generated_by='MANUAL', version=1)
    doc.file_path.save(filename, ContentFile(b'%PDF-1.4 fake'), save=True)
    return doc


class _CapturedSend:
    """Everything the submit path handed to the PayPal multipart transport."""

    def __init__(self):
        self.calls = []

    @property
    def files(self):
        return [f for call in self.calls for f in (call['files'] or [])]

    @property
    def filenames(self):
        return [(f.get('filename') or f.get('name') or '') for f in self.files]


@contextmanager
def _paypal_transport_mocked():
    """Capture outbound PayPal uploads without network; post-submit re-sync
    no-oped."""
    captured = _CapturedSend()

    def fake_post(dispute_id, action, input_json, files):
        captured.calls.append({'dispute_id': dispute_id, 'action': action,
                               'input_json': input_json, 'files': list(files or [])})
        return True, {'ok': True}

    with patch('apps.payments.paypal_disputes_service._post_dispute_action_multipart',
               side_effect=fake_post), \
         patch('apps.payments.paypal_disputes_service.sync_dispute_from_paypal'):
        yield captured


def _attach_pdf_input_tag(html):
    """The full <input ... name="attach_evidence_pdf" ...> tag, or None."""
    match = _ATTACH_PDF_INPUT_RE.search(html)
    return match.group(0) if match else None


class _LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='report_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.user)


class GeneratePersistsAttachFlagsTests(_LoggedInTestCase):
    """Spec 1 — action='generate' must ALSO persist the attach checkboxes."""

    def _post_generate(self, dispute, extra=None):
        payload = {'action': 'generate', 'manager_note': 'stress the service angle'}
        payload.update(extra or {})
        with patch('apps.payments.frontend_views.build_dispute_narrative_notes',
                   return_value=dict(GENERATE_AI_RESULT)):
            resp = self.web.post(
                reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
                payload)
        self.assertEqual(resp.status_code, 302)

    def test_generate_persists_posted_flags_on_new_draft(self):
        dispute = _evidence_open_dispute()
        self._post_generate(dispute, {'attach_evidence_pdf': 'on'})
        draft = dispute.submissions.get(status=DisputeSubmission.STATUS_DRAFT)
        self.assertEqual(draft.notes, 'drafted text')  # sanity: generate branch ran
        self.assertTrue(
            draft.attach_evidence_pdf,
            "generate must persist the posted attach_evidence_pdf='on' tick")
        self.assertFalse(draft.attach_terms,
                         "attach_terms was not posted, so it must be False")
        self.assertFalse(draft.attach_invoice,
                         "attach_invoice was not posted, so it must be False")

    def test_generate_unticks_flags_omitted_from_post(self):
        dispute = _evidence_open_dispute()
        DisputeSubmission.objects.create(
            dispute=dispute, notes='old text', source=DisputeSubmission.SOURCE_MANUAL,
            attach_evidence_pdf=True, attach_terms=True, attach_invoice=True)
        self._post_generate(dispute)  # no checkboxes posted → all unticked
        draft = dispute.submissions.get(status=DisputeSubmission.STATUS_DRAFT)
        self.assertFalse(
            draft.attach_evidence_pdf,
            "an omitted attach_evidence_pdf checkbox means unticked — generate "
            "must not keep the draft's stale True")
        self.assertFalse(draft.attach_terms)
        self.assertFalse(draft.attach_invoice)


class GenerateReportAutoTicksDraftTests(_LoggedInTestCase):
    """Spec 2 — generating the evidence report auto-ticks the working draft."""

    def test_generating_report_ticks_existing_draft(self):
        dispute = _evidence_open_dispute(zd_ticket_id='123')
        draft = DisputeSubmission.objects.create(
            dispute=dispute, notes='draft narrative',
            attach_evidence_pdf=False, attach_terms=False, attach_invoice=False)

        def fake_generate(dispute_id):
            return _report_doc(Dispute.objects.get(pk=dispute_id))

        with patch('apps.payments.frontend_views.generate_evidence_report',
                   side_effect=fake_generate):
            resp = self.web.post(
                reverse('disputes:dispute_generate_documents', args=[dispute.id]))
        self.assertEqual(resp.status_code, 302)
        draft.refresh_from_db()
        self.assertTrue(
            draft.attach_evidence_pdf,
            "generating the report must auto-tick attach_evidence_pdf on the "
            "working DRAFT so the fresh report goes out by default")


class OneStepSendTests(_LoggedInTestCase):
    """Spec 3 — action='send' saves the draft from the POST and submits it to
    PayPal in the same request."""

    def _send(self, dispute, extra=None):
        payload = {'action': 'send', 'manager_note': '',
                   'notes': 'Our narrative for PayPal.'}
        payload.update(extra or {})
        with _paypal_transport_mocked() as captured:
            resp = self.web.post(
                reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
                payload)
        self.assertLess(resp.status_code, 400)
        return resp, captured

    def test_send_without_report_tick_submits_and_omits_pdf(self):
        dispute = _evidence_open_dispute()
        _report_doc(dispute)  # a report exists but is NOT ticked
        resp, captured = self._send(dispute)  # attach_evidence_pdf omitted
        submitted = dispute.submissions.filter(
            status=DisputeSubmission.STATUS_SUBMITTED)
        self.assertEqual(
            submitted.count(), 1,
            "action='send' must save the draft AND submit it in the same request")
        self.assertEqual(submitted.get().notes, 'Our narrative for PayPal.')
        self.assertFalse(
            any('report' in name.lower() for name in captured.filenames),
            "the unticked report must NOT be uploaded — and its absence must not "
            f"block the send; uploaded: {captured.filenames}")

    def test_send_with_report_tick_uploads_the_pdf(self):
        dispute = _evidence_open_dispute()
        doc = _report_doc(dispute)
        resp, captured = self._send(dispute, {'attach_evidence_pdf': 'on'})
        self.assertEqual(
            dispute.submissions.filter(
                status=DisputeSubmission.STATUS_SUBMITTED).count(), 1,
            "action='send' must save the draft AND submit it in the same request")
        stored_basename = doc.file_path.name.rsplit('/', 1)[-1].lower()
        self.assertTrue(
            any('report' in name.lower() or name.lower() == stored_basename
                for name in captured.filenames),
            f"the ticked report PDF must be uploaded; got files: {captured.filenames}")

    def test_send_with_blank_notes_sends_nothing(self):
        dispute = _evidence_open_dispute()
        _report_doc(dispute)
        resp, captured = self._send(dispute, {'notes': '   ',
                                              'attach_evidence_pdf': 'on'})
        self.assertEqual(captured.calls, [],
                         "blank notes must never reach the PayPal transport")
        self.assertFalse(
            dispute.submissions.filter(
                status=DisputeSubmission.STATUS_SUBMITTED).exists(),
            "blank notes must not produce a SUBMITTED submission")


class LegacySubmitUrlTests(_LoggedInTestCase):
    """Spec 4 — the two-step flow keeps working: a saved DRAFT is submitted by
    POSTing the dedicated submit-to-paypal URL."""

    def test_submit_to_paypal_submits_saved_draft(self):
        dispute = _evidence_open_dispute()
        draft = DisputeSubmission.objects.create(
            dispute=dispute, notes='saved draft narrative',
            attach_evidence_pdf=False, attach_terms=False, attach_invoice=False)
        with _paypal_transport_mocked() as captured:
            resp = self.web.post(
                reverse('disputes:dispute_submit_to_paypal', args=[dispute.id]))
        self.assertEqual(resp.status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, DisputeSubmission.STATUS_SUBMITTED)
        self.assertEqual(len(captured.calls), 1)


class ActivityLogAttachmentTests(_LoggedInTestCase):
    """Spec 5 — the EVIDENCE_SENT log line records what was attached."""

    def _latest_sent_log(self, dispute):
        return (DisputeActivityLog.objects
                .filter(dispute=dispute,
                        action=DisputeActivityLog.ACTION_EVIDENCE_SENT)
                .order_by('-performed_at', '-id').first())

    def test_log_mentions_attached_report_filename(self):
        dispute = _evidence_open_dispute()
        _report_doc(dispute)
        DisputeSubmission.objects.create(
            dispute=dispute, notes='narrative with the report attached',
            attach_evidence_pdf=True, attach_terms=False, attach_invoice=False)
        with _paypal_transport_mocked():
            self.web.post(
                reverse('disputes:dispute_submit_to_paypal', args=[dispute.id]))
        log = self._latest_sent_log(dispute)
        self.assertIsNotNone(
            log, "a successful send must write an EVIDENCE_SENT log entry")
        self.assertIn(
            'report', log.details.lower(),
            f"the log must name the attached file(s); got: {log.details!r}")

    def test_log_says_no_attachments_when_none(self):
        dispute = _evidence_open_dispute()  # no report generated at all
        DisputeSubmission.objects.create(
            dispute=dispute, notes='narrative with nothing attached',
            attach_evidence_pdf=False, attach_terms=False, attach_invoice=False)
        with _paypal_transport_mocked():
            self.web.post(
                reverse('disputes:dispute_submit_to_paypal', args=[dispute.id]))
        log = self._latest_sent_log(dispute)
        self.assertIsNotNone(
            log, "a successful send must write an EVIDENCE_SENT log entry")
        self.assertIn(
            'no attachments', log.details.lower(),
            f"an attachment-less send must say so explicitly; got: {log.details!r}")


class DetailPageReportTickDefaultTests(_LoggedInTestCase):
    """Spec 6 — with no working draft, the report tick defaults ON until the
    report has gone out in a SUBMITTED submission; the context exposes
    `report_already_sent`."""

    def _fresh_dispute_with_report(self):
        dispute = _evidence_open_dispute(zd_ticket_id='123')
        _report_doc(dispute)
        return dispute

    def _mark_report_sent(self, dispute):
        DisputeSubmission.objects.create(
            dispute=dispute, notes='already sent with the report',
            attach_evidence_pdf=True, attach_terms=False, attach_invoice=False,
            status=DisputeSubmission.STATUS_SUBMITTED)

    def _get_detail(self, dispute):
        resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_tick_defaults_on_when_report_never_sent(self):
        dispute = self._fresh_dispute_with_report()
        resp = self._get_detail(dispute)
        tag = _attach_pdf_input_tag(resp.content.decode())
        self.assertIsNotNone(tag, "the attach_evidence_pdf checkbox must render")
        self.assertRegex(
            tag, r'\bchecked\b',
            "with a fresh report and no prior send, the tick must default ON")

    def test_tick_defaults_off_once_report_sent(self):
        dispute = self._fresh_dispute_with_report()
        self._mark_report_sent(dispute)
        resp = self._get_detail(dispute)
        tag = _attach_pdf_input_tag(resp.content.decode())
        self.assertIsNotNone(tag, "the attach_evidence_pdf checkbox must render")
        self.assertNotRegex(
            tag, r'\bchecked\b',
            "once the report went out in a SUBMITTED submission, the tick must "
            "not default ON again")

    _MISSING = object()

    def _context_flag(self, resp):
        value = resp.context.get('report_already_sent', self._MISSING)
        self.assertIsNot(value, self._MISSING,
                         "dispute_detail must expose report_already_sent in context")
        return value

    def test_context_flag_false_when_never_sent(self):
        dispute = self._fresh_dispute_with_report()
        resp = self._get_detail(dispute)
        self.assertFalse(self._context_flag(resp))

    def test_context_flag_true_once_sent(self):
        dispute = self._fresh_dispute_with_report()
        self._mark_report_sent(dispute)
        resp = self._get_detail(dispute)
        self.assertTrue(self._context_flag(resp))


class FirstTimeWarningCopyTests(_LoggedInTestCase):
    """Spec 7 — the send-confirmation copy shows only for the FIRST send of a
    generated report."""

    def test_copy_present_when_report_exists_and_unsent(self):
        dispute = _evidence_open_dispute(zd_ticket_id='123')
        _report_doc(dispute)
        resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        self.assertContains(resp, SEND_WITHOUT_REPORT_COPY)

    def test_copy_absent_once_report_sent(self):
        dispute = _evidence_open_dispute(zd_ticket_id='123')
        _report_doc(dispute)
        DisputeSubmission.objects.create(
            dispute=dispute, notes='already sent', attach_evidence_pdf=True,
            attach_terms=False, attach_invoice=False,
            status=DisputeSubmission.STATUS_SUBMITTED)
        resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        self.assertNotContains(resp, SEND_WITHOUT_REPORT_COPY)

    def test_copy_absent_when_no_report_exists(self):
        dispute = _evidence_open_dispute(zd_ticket_id='123')
        resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        self.assertNotContains(resp, SEND_WITHOUT_REPORT_COPY)
