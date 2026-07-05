"""Red-phase spec — backfill: send the evidence-report PDF as a PayPal
follow-up for disputes whose earlier replies went out WITHOUT the report.

Before the composer defaulted the report tick ON, managers sent replies with
attach_evidence_pdf unticked, so live cases have a generated report that PayPal
never saw. This file pins the NEW backfill (deliberately unimplemented — these
tests must fail until it lands):

- Service: `send_missing_dispute_reports(*, dry_run=False)` in
  apps.payments.paypal_disputes_service, returning a summary dict with the
  keys `candidates`, `sent`, `failed`, `skipped_draft`.
- A dispute is a CANDIDATE iff ALL hold:
    1. it has an EVIDENCE_REPORT document with a non-empty stored file;
    2. it is engaged — at least one SUBMITTED submission — and NO SUBMITTED
       submission carried the report (attach_evidence_pdf=True);
    3. its reply window is open (dispute.submit_endpoint is non-empty);
    4. it has no open DRAFT submission (a manager may be mid-work) — those
       are skipped and counted in `skipped_draft`.
- Each candidate gets ONE new submission pushed through the existing PayPal
  machinery: SUBMITTED, kind SUPPORTING_INFO, attach_evidence_pdf=True,
  attach_terms/attach_invoice OFF (those went out with the original reply),
  non-empty notes under PayPal's 2000-char limit, and the multipart upload
  actually carries the report file via action 'provide-supporting-info'.
- dry_run counts candidates but sends nothing; one dispute's transport
  failure never stops the run; a second run is a no-op (the sent report
  makes report-already-sent true).
- Thin management command `send_missing_dispute_reports` (mirroring
  backfill_claim_dates) supports --dry-run.

Transport is mocked at apps.payments.paypal_disputes_service
._post_dispute_action_multipart — nothing here talks to PayPal.
"""

import itertools
from contextlib import contextmanager
from datetime import datetime, timezone as dt_tz
from io import StringIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase

from apps.payments import paypal_disputes_service as pds
from apps.payments.models import Dispute, DisputeDocument, DisputeSubmission

_ids = itertools.count(1)


def _dispute(payload=None, **kw):
    base = dict(paypal_dispute_id=f'PP-D-BFR{next(_ids)}', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED', status='MATCHED',
                raw_webhook_payload=payload or {})
    base.update(kw)
    return Dispute.objects.create(**base)


def _under_review_dispute(**kw):
    """A dispute whose reply window is open for follow-ups
    (dispute.submit_endpoint == 'provide-supporting-info')."""
    return _dispute(payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'}, **kw)


def _report_doc(dispute, filename='report.pdf'):
    """A generated EVIDENCE_REPORT document with a real file on disk."""
    doc = DisputeDocument.objects.create(
        dispute=dispute, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
        status=DisputeDocument.STATUS_DRAFT, generated_by='MANUAL', version=1)
    doc.file_path.save(filename, ContentFile(b'%PDF-1.4 fake'), save=True)
    return doc


def _submitted_reply(dispute, *, with_report=False, notes='earlier reply'):
    """An earlier reply that already went out to PayPal (created directly via
    the ORM — its transport round-trip is history)."""
    return DisputeSubmission.objects.create(
        dispute=dispute, notes=notes,
        status=DisputeSubmission.STATUS_SUBMITTED,
        kind=DisputeSubmission.KIND_EVIDENCE,
        attach_evidence_pdf=with_report)


def _candidate(**kw):
    """A dispute the backfill MUST pick up: reply window open, engaged with one
    earlier SUBMITTED reply that did NOT carry the report, and a generated
    evidence-report PDF on file. Returns (dispute, earlier_reply)."""
    dispute = _under_review_dispute(**kw)
    _report_doc(dispute)
    reply = _submitted_reply(dispute)
    return dispute, reply


class _CapturedSend:
    """Everything the backfill handed to the PayPal multipart transport."""

    def __init__(self):
        self.calls = []

    @property
    def files(self):
        return [f for call in self.calls for f in (call['files'] or [])]

    @property
    def filenames(self):
        return [(f.get('filename') or f.get('name') or '') for f in self.files]


@contextmanager
def _paypal_transport_mocked(result_for=None):
    """Capture outbound PayPal uploads without network; post-submit re-sync
    no-oped. `result_for(dispute_id)` may vary the transport result per
    dispute; the default always answers (True, {'ok': True})."""
    captured = _CapturedSend()

    def fake_post(dispute_id, action, input_json, files):
        captured.calls.append({'dispute_id': dispute_id, 'action': action,
                               'input_json': input_json, 'files': list(files or [])})
        if result_for is not None:
            return result_for(dispute_id)
        return True, {'ok': True}

    with patch('apps.payments.paypal_disputes_service._post_dispute_action_multipart',
               side_effect=fake_post), \
         patch('apps.payments.paypal_disputes_service.sync_dispute_from_paypal'):
        yield captured


class _BackfillTestCase(TestCase):
    def run_backfill(self, *, dry_run=False, result_for=None):
        with _paypal_transport_mocked(result_for) as captured:
            summary = pds.send_missing_dispute_reports(dry_run=dry_run)
        return summary, captured

    def assertSummary(self, summary, *, candidates, sent, failed=0, skipped_draft=0):
        expected = {'candidates': candidates, 'sent': sent,
                    'failed': failed, 'skipped_draft': skipped_draft}
        for key, want in expected.items():
            self.assertIn(key, summary,
                          f"the summary must report {key!r}; got {summary!r}")
            self.assertEqual(summary[key], want,
                             f"summary[{key!r}] must be {want}; got {summary!r}")


class CandidateSendTests(_BackfillTestCase):
    """A full candidate gets exactly ONE new follow-up submission that pushes
    the report through the existing PayPal machinery."""

    def _run_one(self):
        dispute, earlier = _candidate()
        summary, captured = self.run_backfill()
        new_qs = dispute.submissions.exclude(pk=earlier.pk)
        self.assertEqual(
            new_qs.count(), 1,
            "the backfill must create exactly one new submission per candidate")
        return dispute, summary, captured, new_qs.get()

    def test_summary_counts_the_send(self):
        _, summary, _, _ = self._run_one()
        self.assertSummary(summary, candidates=1, sent=1, failed=0, skipped_draft=0)

    def test_new_submission_is_submitted_supporting_info_with_report_ticked(self):
        _, _, _, new = self._run_one()
        self.assertEqual(new.status, DisputeSubmission.STATUS_SUBMITTED)
        self.assertEqual(new.kind, DisputeSubmission.KIND_SUPPORTING_INFO)
        self.assertTrue(new.attach_evidence_pdf,
                        "the whole point of the backfill is sending the report")

    def test_new_submission_notes_are_nonempty_and_under_paypal_limit(self):
        _, _, _, new = self._run_one()
        self.assertTrue(new.notes.strip(),
                        "the follow-up must carry a note for PayPal")
        self.assertLess(len(new.notes), 2000,
                        "PayPal rejects notes of 2000+ chars "
                        "(NOTE_CAN_NOT_BE_MORE_THAN_2000_CHARS)")

    def test_upload_carries_the_report_pdf(self):
        _, _, captured, _ = self._run_one()
        self.assertEqual(len(captured.calls), 1,
                         "one candidate means one transport call")
        self.assertTrue(
            any('report' in name.lower() for name in captured.filenames),
            f"the report PDF must be in the multipart upload; got {captured.filenames}")

    def test_terms_and_invoice_are_not_attached(self):
        _, _, captured, new = self._run_one()
        self.assertFalse(new.attach_terms,
                         "T&C went out with the original reply — do not resend")
        self.assertFalse(new.attach_invoice,
                         "the invoice went out with the original reply — do not resend")
        self.assertEqual(len(captured.files), 1,
                         f"only the report goes out; got {captured.filenames}")

    def test_action_is_provide_supporting_info_for_under_review_dispute(self):
        dispute, _, captured, _ = self._run_one()
        self.assertTrue(captured.calls,
                        "the transport must be called for a candidate")
        self.assertEqual(captured.calls[0]['action'], 'provide-supporting-info',
                         "an UNDER_PAYPAL_REVIEW dispute takes the follow-up endpoint")
        self.assertEqual(captured.calls[0]['dispute_id'], dispute.paypal_dispute_id)


class NonCandidatesUntouchedTests(_BackfillTestCase):
    """Disputes failing any selection rule are left completely alone — no new
    submission, and the transport is never called for them."""

    def _assert_untouched(self, disputes_with_counts):
        summary, captured = self.run_backfill()
        self.assertSummary(summary, candidates=0, sent=0, failed=0, skipped_draft=0)
        self.assertEqual(captured.calls, [],
                         "the transport must never be called for non-candidates")
        for dispute, expected_subs in disputes_with_counts:
            self.assertEqual(
                dispute.submissions.count(), expected_subs,
                f"dispute {dispute.paypal_dispute_id} must gain no new submission")

    def test_report_already_sent_is_left_alone(self):
        dispute = _under_review_dispute()
        _report_doc(dispute)
        _submitted_reply(dispute, with_report=False, notes='first reply, no report')
        _submitted_reply(dispute, with_report=True, notes='second reply WITH the report')
        self._assert_untouched([(dispute, 2)])

    def test_never_engaged_dispute_is_left_alone(self):
        # A report exists but nothing was ever submitted — this is not a
        # missing follow-up, it is a case the manager has not answered yet.
        dispute = _under_review_dispute()
        _report_doc(dispute)
        self._assert_untouched([(dispute, 0)])

    def test_closed_reply_window_is_left_alone(self):
        # Closed on our side: LORA terminal status.
        won = _dispute(status='RESOLVED_WON',
                       payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        _report_doc(won)
        _submitted_reply(won)
        # Closed on PayPal's side: the payload says RESOLVED.
        resolved = _dispute(payload={'status': 'RESOLVED'})
        _report_doc(resolved)
        _submitted_reply(resolved)
        self._assert_untouched([(won, 1), (resolved, 1)])

    def test_missing_or_empty_report_doc_is_left_alone(self):
        # Engaged and open, but there is no report to send.
        no_doc = _under_review_dispute()
        _submitted_reply(no_doc)
        # A document row without a stored file is just as unsendable.
        empty_doc = _under_review_dispute()
        DisputeDocument.objects.create(
            dispute=empty_doc, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
            status=DisputeDocument.STATUS_DRAFT, generated_by='MANUAL', version=1)
        _submitted_reply(empty_doc)
        self._assert_untouched([(no_doc, 1), (empty_doc, 1)])


class OpenDraftSkipTests(_BackfillTestCase):
    """A manager may be mid-work: an open DRAFT parks the dispute in
    `skipped_draft` and the backfill must not touch it."""

    def test_open_draft_dispute_is_skipped_and_counted(self):
        dispute, _earlier = _candidate()
        draft = DisputeSubmission.objects.create(
            dispute=dispute, notes='manager is mid-edit',
            status=DisputeSubmission.STATUS_DRAFT)
        summary, captured = self.run_backfill()
        self.assertSummary(summary, candidates=0, sent=0, failed=0, skipped_draft=1)
        self.assertEqual(captured.calls, [],
                         "a skipped dispute must never reach the transport")
        self.assertEqual(dispute.submissions.count(), 2,
                         "no new submission for a skipped dispute")
        draft.refresh_from_db()
        self.assertEqual(draft.status, DisputeSubmission.STATUS_DRAFT,
                         "the manager's working draft must be left untouched")


class DryRunTests(_BackfillTestCase):
    def test_dry_run_counts_candidates_but_sends_nothing(self):
        dispute, _earlier = _candidate()
        summary, captured = self.run_backfill(dry_run=True)
        self.assertSummary(summary, candidates=1, sent=0, failed=0, skipped_draft=0)
        self.assertEqual(captured.calls, [],
                         "dry_run must never hit the PayPal transport")
        self.assertEqual(dispute.submissions.count(), 1,
                         "dry_run must not create submissions")


class FailureIsolationTests(_BackfillTestCase):
    def test_one_transport_failure_does_not_stop_the_run(self):
        d_fail, fail_reply = _candidate(paypal_dispute_id='PP-D-BFR-FAIL')
        d_ok, ok_reply = _candidate(paypal_dispute_id='PP-D-BFR-OK')

        def result_for(dispute_id):
            if dispute_id == 'PP-D-BFR-FAIL':
                return False, {'error': 'boom'}
            return True, {'ok': True}

        summary, _captured = self.run_backfill(result_for=result_for)
        self.assertSummary(summary, candidates=2, sent=1, failed=1, skipped_draft=0)

        failed_new = d_fail.submissions.exclude(pk=fail_reply.pk)
        self.assertEqual(failed_new.count(), 1)
        self.assertEqual(failed_new.get().status, DisputeSubmission.STATUS_FAILED,
                         "the failed send must be recorded as FAILED for retry")

        ok_new = d_ok.submissions.exclude(pk=ok_reply.pk)
        self.assertEqual(ok_new.count(), 1)
        self.assertEqual(ok_new.get().status, DisputeSubmission.STATUS_SUBMITTED,
                         "the other candidate must still be processed")


class IdempotencyTests(_BackfillTestCase):
    def test_running_twice_sends_only_once(self):
        dispute, _earlier = _candidate()

        first, _ = self.run_backfill()
        self.assertSummary(first, candidates=1, sent=1, failed=0, skipped_draft=0)

        second, captured2 = self.run_backfill()
        self.assertSummary(second, candidates=0, sent=0, failed=0, skipped_draft=0)
        self.assertEqual(captured2.calls, [],
                         "the second run must not touch the transport")
        self.assertEqual(dispute.submissions.count(), 2,
                         "earlier reply + the one backfill send — and nothing more")


class ManagementCommandTests(TestCase):
    """The thin `send_missing_dispute_reports` command (mirroring
    backfill_claim_dates) exists and honours --dry-run."""

    def test_command_dry_run_reports_and_sends_nothing(self):
        dispute, _earlier = _candidate()
        out = StringIO()
        with _paypal_transport_mocked() as captured:
            call_command('send_missing_dispute_reports', '--dry-run', stdout=out)
        self.assertEqual(captured.calls, [],
                         "--dry-run must never hit the PayPal transport")
        self.assertEqual(dispute.submissions.count(), 1,
                         "--dry-run must not create submissions")
        self.assertIn('candidate', out.getvalue().lower(),
                      "the command must surface the candidate count to the operator")
