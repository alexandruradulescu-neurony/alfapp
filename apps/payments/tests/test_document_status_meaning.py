"""Red-phase spec — DisputeDocument.status must actually mean something
(deliberately unimplemented for most items below; these tests must fail
until the behaviour lands).

Today `DisputeDocument.status` is set to DRAFT at creation and is NEVER
changed by any code path afterwards — not by submit_dispute_response, not by
anything else. The documents table always reads "Draft", even for a report
that was in fact sent to PayPal weeks ago. A manager who just saved an edit
to a report reads that unchanging "Draft" label as "my edit didn't save".

NEW behaviour pinned here:

1. A successful submit_dispute_response() for a submission with
   attach_evidence_pdf=True marks the EVIDENCE_REPORT document that was
   actually attached (the one attached_evidence_report() returns)
   STATUS_SENT — WITHOUT bumping its updated_at (so it doesn't also start
   looking freshly "edited") or its version. Other documents of the same
   dispute are left DRAFT.
2. attach_evidence_pdf=False, or a clean PayPal rejection, must leave every
   document's status untouched (guards for the same code path — largely
   trivially true today, since nothing ever changes status yet, but must
   keep holding once item 1 is implemented).
3. The documents table never shows the bare "Draft" label for a report: a
   DRAFT report's status cell reads exactly "Not sent yet"; a SENT report's
   reads exactly "Sent to PayPal".
4. A SENT report's row drops its Edit link and disables its Delete button;
   a DRAFT report keeps both enabled.
5. Sending again later (a second, later, successful submission) that
   attaches the SAME report is idempotent: it stays SENT, no error.
6. Regression guard for whatever implements item 1: attached_evidence_report
   must keep choosing purely by updated_at — it must NOT start excluding
   SENT documents, or a second reply (item 5) would find nothing to attach.
"""

import re
from contextlib import contextmanager
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.payments import paypal_disputes_service as pds
from apps.payments.models import Dispute, DisputeDocument, DisputeSubmission

User = get_user_model()


def _dispute(payload=None, **kw):
    base = dict(paypal_dispute_id='PP-D-DOCSTATUS', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED',
                raw_webhook_payload=payload or {})
    base.update(kw)
    return Dispute.objects.create(**base)


def _evidence_open_dispute(**kw):
    """A dispute PayPal accepts a first-evidence submission for
    (dispute.submit_endpoint == 'provide-evidence')."""
    return _dispute(payload={'dispute_state': 'REQUIRED_ACTION'},
                    dispute_life_cycle_stage='CHARGEBACK', **kw)


def _report_doc(dispute, version=1, filename=None, status=DisputeDocument.STATUS_DRAFT):
    """A generated EVIDENCE_REPORT document with a real file on disk."""
    doc = DisputeDocument.objects.create(
        dispute=dispute, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
        status=status, generated_by='MANUAL', version=version)
    doc.file_path.save(filename or f'report_v{version}.pdf', ContentFile(b'%PDF-1.4 fake'), save=True)
    return doc


@contextmanager
def _paypal_success():
    """submit_dispute_response's PayPal transport patched to succeed, with the
    post-submit re-sync no-oped (no network in tests). Mirrors
    test_dispute_submission.SubmitOrchestrationTests."""
    with patch.object(pds, 'provide_evidence_files', return_value=(True, {'ok': True})), \
         patch.object(pds, 'sync_dispute_from_paypal'):
        yield


@contextmanager
def _paypal_rejection():
    """Same, but the transport cleanly rejects (ok=False, no exception) —
    mirrors PayPal returning e.g. an EVIDENCE_TYPE_IS_NOT_ALLOWED error."""
    with patch.object(pds, 'provide_evidence_files',
                       return_value=(False, {'error': 'REJECTED'})), \
         patch.object(pds, 'sync_dispute_from_paypal'):
        yield


# ---------------------------------------------------------------------------
# 1. A successful, attached submit marks the report SENT
# ---------------------------------------------------------------------------

class MarkSentOnSuccessfulSubmitTests(TestCase):
    """Item 1 — the ATTACHED report (and only that one) becomes SENT after a
    successful submit; updated_at and version must not move."""

    def test_attached_report_marked_sent_after_successful_submit(self):
        d = _evidence_open_dispute()
        # Created first, so it is NOT the most-recently-touched report and
        # must be left alone.
        doc_other = _report_doc(d, version=1, filename='other.pdf')
        # Created second, so attached_evidence_report() picks this one.
        doc_attached = _report_doc(d, version=2, filename='attached.pdf')
        # Sanity: confirms the test's premise before asserting new behaviour.
        self.assertEqual(pds.attached_evidence_report(d), doc_attached)

        updated_before = doc_attached.updated_at
        version_before = doc_attached.version
        sub = DisputeSubmission.objects.create(dispute=d, notes='our case',
                                               attach_evidence_pdf=True)

        with _paypal_success():
            ok = pds.submit_dispute_response(sub, performed_by=None)
        self.assertTrue(ok, "expected the (transport-patched) submit to succeed")

        doc_attached.refresh_from_db()
        doc_other.refresh_from_db()
        self.assertEqual(
            doc_attached.status, DisputeDocument.STATUS_SENT,
            "the report actually attached to a successful submit must read SENT")
        self.assertEqual(
            doc_attached.updated_at, updated_before,
            "marking SENT must not bump updated_at — that would make an untouched "
            "report look freshly edited")
        self.assertEqual(
            doc_attached.version, version_before,
            "marking SENT must not bump version")
        self.assertEqual(
            doc_other.status, DisputeDocument.STATUS_DRAFT,
            "a document that was NOT attached to this submission must stay DRAFT")


# ---------------------------------------------------------------------------
# 2. No status change when not attached, or on a clean rejection
# ---------------------------------------------------------------------------

class NoStatusChangeWhenNotAttachedOrRejectedTests(TestCase):
    """Item 2 — guards that must hold both today and after item 1 lands."""

    def test_attach_flag_off_leaves_status_untouched(self):
        d = _evidence_open_dispute()
        doc = _report_doc(d)
        sub = DisputeSubmission.objects.create(dispute=d, notes='n',
                                               attach_evidence_pdf=False)
        with _paypal_success():
            ok = pds.submit_dispute_response(sub, performed_by=None)
        self.assertTrue(ok)
        doc.refresh_from_db()
        self.assertEqual(
            doc.status, DisputeDocument.STATUS_DRAFT,
            "attach_evidence_pdf was off — no document should be marked SENT")

    def test_paypal_rejection_leaves_status_untouched(self):
        d = _evidence_open_dispute()
        doc = _report_doc(d)
        sub = DisputeSubmission.objects.create(dispute=d, notes='n',
                                               attach_evidence_pdf=True)
        with _paypal_rejection():
            ok = pds.submit_dispute_response(sub, performed_by=None)
        self.assertFalse(ok, "the transport was patched to reject")
        doc.refresh_from_db()
        self.assertEqual(
            doc.status, DisputeDocument.STATUS_DRAFT,
            "a clean PayPal rejection must not mark the report SENT")


# ---------------------------------------------------------------------------
# 3. Detail-page wording: never bare "Draft" for a report
# ---------------------------------------------------------------------------

class _LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='docstatus_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.user)


def _rows_matching(html, needle):
    """<tr>-delimited row fragments containing needle (mirrors
    test_dispute_editing_safety._rows_matching — table rows have no id, so
    tests tell rows apart by a distinguishing marker like >v{{ version }}<)."""
    return [r for r in html.split('<tr>') if needle in r]


def _bounded_row(html, needle):
    """The single row matching `needle`, trimmed to its OWN closing </tr>.

    A plain <tr>-split (as above) is fine for a row that has a sibling <tr>
    right after it, but the LAST row in a table has no such sibling — the
    naive fragment would otherwise run on into whatever the next <tr>
    anywhere later on the page is (a different table entirely), which would
    make an "exactly this text" assertion unreliable. Cutting at the row's
    own </tr> keeps the fragment scoped to just this row's cells.
    """
    rows = _rows_matching(html, needle)
    assert len(rows) == 1, f"expected exactly one row matching {needle!r}, found {len(rows)}"
    return rows[0].split('</tr>')[0]


def _status_cell_text(row_html):
    """The primary status-badge text: the first <span> inside the Status
    <td> (the second <td> in the row — Type, Status, Version, Generated By,
    Created, Actions). The optional "Attached to the next reply" marker is a
    SECOND, separate <span> in the same cell and is deliberately not part of
    this — it is an orthogonal concern already covered by
    test_dispute_editing_safety.AttachedDocumentMarkerTests.
    """
    cells = re.findall(r'<td\b[^>]*>(.*?)</td>', row_html, re.IGNORECASE | re.DOTALL)
    assert len(cells) >= 2, f"expected at least 2 <td> cells in row, got {len(cells)}: {row_html!r}"
    status_cell = cells[1]
    m = re.search(r'<span\b[^>]*>(.*?)</span>', status_cell, re.IGNORECASE | re.DOTALL)
    text = m.group(1) if m else status_cell
    return text.strip()


class DocumentStatusWordingTests(_LoggedInTestCase):
    """Item 3 — the documents table never shows the bare 'Draft' label for a
    report."""

    def test_draft_report_reads_not_sent_yet(self):
        d = _dispute()
        _report_doc(d, version=11, status=DisputeDocument.STATUS_DRAFT)
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        html = resp.content.decode()
        row = _bounded_row(html, '>v11<')
        self.assertNotIn('>Draft<', row,
                         "the bare 'Draft' label must never show for a report")
        self.assertEqual(
            _status_cell_text(row), 'Not sent yet',
            f"a DRAFT report's status cell must read exactly 'Not sent yet'; row: {row!r}")

    def test_sent_report_reads_sent_to_paypal(self):
        d = _dispute()
        _report_doc(d, version=12, status=DisputeDocument.STATUS_SENT)
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        html = resp.content.decode()
        row = _bounded_row(html, '>v12<')
        self.assertEqual(
            _status_cell_text(row), 'Sent to PayPal',
            f"a SENT report's status cell must read exactly 'Sent to PayPal'; row: {row!r}")


# ---------------------------------------------------------------------------
# 4. SENT drops Edit + disables Delete; DRAFT keeps both
# ---------------------------------------------------------------------------

class SentDocumentRowControlsTests(_LoggedInTestCase):
    """Item 4 — a SENT report's row drops the Edit link and disables Delete;
    a DRAFT report keeps both enabled."""

    def _delete_button_tag(self, row_html):
        m = re.search(r'<button\b[^>]*title="Delete Document"[^>]*>', row_html, re.IGNORECASE)
        self.assertIsNotNone(m, f"expected a Delete Document button in row: {row_html!r}")
        return m.group(0)

    def _has_disabled_attribute(self, tag):
        """True if `tag` carries a real boolean `disabled` HTML attribute —
        NOT merely a Tailwind `disabled:`-variant class like
        `disabled:opacity-50` (the button's class="..." always carries those,
        SENT or not, so a naive 'disabled' in tag substring check is a false
        positive on every row — this strips the class attribute first)."""
        without_class = re.sub(r'\bclass="[^"]*"', '', tag, flags=re.IGNORECASE)
        return re.search(r'(?:^|\s)disabled(?:\s|=|>|$)', without_class, re.IGNORECASE) is not None

    def test_sent_report_has_no_edit_link_and_disabled_delete(self):
        d = _dispute()
        doc = _report_doc(d, version=21, status=DisputeDocument.STATUS_SENT)
        edit_url = reverse('disputes:dispute_edit_document', args=[doc.id])
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        html = resp.content.decode()
        row = _bounded_row(html, '>v21<')
        self.assertNotIn(edit_url, row, "a SENT report must not offer an Edit link")
        delete_btn = self._delete_button_tag(row)
        self.assertTrue(
            self._has_disabled_attribute(delete_btn),
            f"a SENT report's Delete button must carry a real disabled attribute: {delete_btn}")

    def test_draft_report_keeps_edit_link_and_enabled_delete(self):
        d = _dispute()
        doc = _report_doc(d, version=22, status=DisputeDocument.STATUS_DRAFT)
        edit_url = reverse('disputes:dispute_edit_document', args=[doc.id])
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        html = resp.content.decode()
        row = _bounded_row(html, '>v22<')
        self.assertIn(edit_url, row, "a DRAFT report must still offer an Edit link")
        delete_btn = self._delete_button_tag(row)
        self.assertFalse(
            self._has_disabled_attribute(delete_btn),
            f"a DRAFT report's Delete button must not be disabled: {delete_btn}")


# ---------------------------------------------------------------------------
# 5. Sending again later keeps the same report SENT (idempotent)
# ---------------------------------------------------------------------------

class SendingAgainStaysSentTests(TestCase):
    """Item 5 — a second, later, successful submission that attaches the
    SAME report must not error, and must leave it SENT."""

    def test_second_successful_submission_keeps_the_report_sent(self):
        d = _evidence_open_dispute()
        doc = _report_doc(d)
        sub1 = DisputeSubmission.objects.create(dispute=d, notes='first',
                                                attach_evidence_pdf=True)
        with _paypal_success():
            ok1 = pds.submit_dispute_response(sub1, performed_by=None)
        self.assertTrue(ok1)
        doc.refresh_from_db()
        self.assertEqual(doc.status, DisputeDocument.STATUS_SENT)

        sub2 = DisputeSubmission.objects.create(dispute=d, notes='second',
                                                attach_evidence_pdf=True)
        with _paypal_success():
            ok2 = pds.submit_dispute_response(sub2, performed_by=None)
        self.assertTrue(
            ok2, "a second, later submission attaching the SAME already-SENT "
            "report must not error")
        doc.refresh_from_db()
        self.assertEqual(
            doc.status, DisputeDocument.STATUS_SENT,
            "the report must remain SENT after being (re-)attached to a second reply")


# ---------------------------------------------------------------------------
# 6. Regression guard: attached_evidence_report ignores status
# ---------------------------------------------------------------------------

class AttachedEvidenceReportIgnoresStatusTests(TestCase):
    """Item 6 — regression guard for whatever implements item 1: picking the
    report to attach must stay a pure updated_at ordering and must NOT start
    excluding SENT documents (otherwise item 5's second reply would find
    nothing to attach)."""

    def test_a_sent_report_is_still_picked_when_most_recently_touched(self):
        d = _dispute()
        older_draft = _report_doc(d, version=1, filename='older.pdf')
        newer_sent = _report_doc(d, version=2, filename='newer.pdf',
                                 status=DisputeDocument.STATUS_SENT)
        # Sanity: created after -> naturally the latest updated_at too.
        self.assertGreater(newer_sent.updated_at, older_draft.updated_at)

        result = pds.attached_evidence_report(d)
        self.assertEqual(
            result, newer_sent,
            "attached_evidence_report must pick by updated_at regardless of status "
            "— a SENT report must remain eligible to be attached again")
