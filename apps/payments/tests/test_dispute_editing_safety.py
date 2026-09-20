"""Red-phase spec — dispute-reply editing safety (deliberately unimplemented;
these tests must fail until the behaviour lands):

1. Once an evidence report exists, EVERY "Generate evidence report" form on the
   dispute detail page asks for confirmation before overwriting it (no report
   yet -> no confirmation).
2. The report a submission actually attaches is the most recently EDITED
   EVIDENCE_REPORT document (latest updated_at), not the most recently
   created one.
3. The documents table marks the row of the document that would attach next.
4. "Draft with AI" confirms before it overwrites whatever is in the composer.
5. Pressing Enter in the composer must SAVE, not re-run "Draft with AI" — the
   first submit button inside the composer form must be the Save button.
6. A PayPal rejection (ok=False, no exception) of a one-step 'send' must not
   strand the manager: the FAILED submission stays as an audit record, and a
   fresh, identical DRAFT is created so they can fix and resend without
   retyping everything.
7. generate_evidence_report's generated_by must honestly reflect whether the
   AI narrative grouping actually ran, instead of being hardcoded to MANUAL.
9. The documents table shows when a document was edited after creation, and
   drops the Edit link once a document is SENT (immutable history).

(Item 8 — the narrative input window — lives in test_narrative_input_window.py.)
"""

import re
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.defaultfilters import date as date_filter
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.claims.models import Claim
from apps.payments import document_service as ds
from apps.payments import frontend_views as fv
from apps.payments import paypal_disputes_service as pds
from apps.payments.models import Dispute, DisputeDocument, DisputeSubmission

User = get_user_model()

ATTACHED_MARKER = 'Attached to the next reply'


def _dispute(payload=None, **kw):
    base = dict(paypal_dispute_id='PP-D-EDITSAFE', buyer_email='b@example.com',
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


def _report_doc(dispute, version=1, filename=None):
    """A generated EVIDENCE_REPORT document with a real file on disk."""
    doc = DisputeDocument.objects.create(
        dispute=dispute, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
        status=DisputeDocument.STATUS_DRAFT, generated_by='MANUAL', version=version)
    doc.file_path.save(filename or f'report_v{version}.pdf', ContentFile(b'%PDF-1.4 fake'), save=True)
    return doc


def _touch(doc, *, created_at=None, updated_at=None):
    """Force created_at/updated_at via a raw update() — auto_now/auto_now_add
    only fire on Model.save(), never on QuerySet.update()."""
    fields = {}
    if created_at is not None:
        fields['created_at'] = created_at
    if updated_at is not None:
        fields['updated_at'] = updated_at
    DisputeDocument.objects.filter(pk=doc.pk).update(**fields)
    doc.refresh_from_db()
    return doc


class _LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='editsafe_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.user)


def _forms_with_action(html, action_url):
    """Every <form ...> opening tag whose action attribute equals action_url."""
    pattern = re.compile(
        r'<form\b[^>]*\baction=["\']' + re.escape(action_url) + r'["\'][^>]*>',
        re.IGNORECASE)
    return pattern.findall(html)


def _button_tags(html):
    return re.findall(r'<button\b[^>]*>', html, re.IGNORECASE)


def _find_button(html, **attrs):
    """The first <button ...> opening tag carrying every given name="value"
    attribute pair (order-independent, either quote style)."""
    for tag in _button_tags(html):
        if all(f'{k}="{v}"' in tag or f"{k}='{v}'" in tag for k, v in attrs.items()):
            return tag
    return None


def _rows_matching(html, needle):
    """<tr>-delimited row fragments containing needle (table rows have no id,
    so tests tell rows apart by a distinguishing marker like >v{{ version }}<)."""
    return [r for r in html.split('<tr>') if needle in r]


# ---------------------------------------------------------------------------
# 1. Generate needs confirmation once a report exists
# ---------------------------------------------------------------------------

class GenerateConfirmationTests(_LoggedInTestCase):
    """Item 1 — every generate-documents form confirms once a report exists;
    none do while there isn't one yet."""

    def test_no_confirm_when_no_report_exists(self):
        d = _dispute()
        action_url = reverse('disputes:dispute_generate_documents', args=[d.id])
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        forms = _forms_with_action(resp.content.decode(), action_url)
        self.assertGreaterEqual(len(forms), 1, "expected at least one generate-documents form")
        for tag in forms:
            self.assertNotIn('confirm(', tag,
                             f"no report exists yet — this form must not confirm: {tag}")

    def test_confirm_required_once_a_report_exists(self):
        d = _dispute()
        _report_doc(d)
        action_url = reverse('disputes:dispute_generate_documents', args=[d.id])
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        forms = _forms_with_action(resp.content.decode(), action_url)
        self.assertGreaterEqual(len(forms), 1, "expected at least one generate-documents form")
        for tag in forms:
            self.assertIn('onsubmit', tag.lower(), f"missing onsubmit confirm: {tag}")
            self.assertIn('confirm(', tag, f"missing onsubmit confirm: {tag}")


# ---------------------------------------------------------------------------
# 2. The attached report is the most recently TOUCHED one
# ---------------------------------------------------------------------------

class LatestTouchedReportAttachmentTests(TestCase):
    """Item 2 — _build_submission_files must choose the EVIDENCE_REPORT with
    the latest updated_at, not the latest created_at."""

    def test_build_submission_files_picks_latest_updated_not_latest_created(self):
        d = _dispute()
        doc_a = _report_doc(d, version=1, filename='a.pdf')
        doc_b = _report_doc(d, version=2, filename='b.pdf')
        now = timezone.now()
        # B was CREATED after A, but A was EDITED (touched) more recently.
        _touch(doc_a, created_at=now - timedelta(hours=1), updated_at=now)
        _touch(doc_b, created_at=now - timedelta(minutes=30), updated_at=now - timedelta(minutes=30))
        self.assertGreater(doc_b.created_at, doc_a.created_at)   # sanity: B created after A
        self.assertGreater(doc_a.updated_at, doc_b.updated_at)   # sanity: A touched more recently

        sub = DisputeSubmission.objects.create(dispute=d, notes='n', attach_evidence_pdf=True)
        files = pds._build_submission_files(sub)

        self.assertEqual(len(files), 1)
        a_basename = doc_a.file_path.name.rsplit('/', 1)[-1]
        b_basename = doc_b.file_path.name.rsplit('/', 1)[-1]
        self.assertEqual(
            files[0]['filename'], a_basename,
            f"expected the most recently EDITED report ({a_basename!r}) attached, got "
            f"{files[0]['filename']!r} — still ordering by created_at instead of updated_at")
        self.assertNotEqual(files[0]['filename'], b_basename)


# ---------------------------------------------------------------------------
# 3. The detail page marks the document that would be attached next
# ---------------------------------------------------------------------------

class AttachedDocumentMarkerTests(_LoggedInTestCase):
    """Item 3 — the documents table row of the document _build_submission_files
    would actually pick shows the ATTACHED_MARKER, exactly once on the page."""

    def test_marks_only_the_most_recently_edited_report_row(self):
        d = _dispute()
        doc_a = _report_doc(d, version=1, filename='a.pdf')
        doc_b = _report_doc(d, version=2, filename='b.pdf')
        now = timezone.now()
        _touch(doc_a, created_at=now - timedelta(hours=1), updated_at=now)
        _touch(doc_b, created_at=now - timedelta(minutes=30), updated_at=now - timedelta(minutes=30))

        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        html = resp.content.decode()
        self.assertEqual(
            html.count(ATTACHED_MARKER), 1,
            f"expected {ATTACHED_MARKER!r} exactly once on the page; "
            f"found {html.count(ATTACHED_MARKER)}")
        row_a = _rows_matching(html, '>v1<')
        row_b = _rows_matching(html, '>v2<')
        self.assertEqual(len(row_a), 1, "expected exactly one row for v1")
        self.assertEqual(len(row_b), 1, "expected exactly one row for v2")
        self.assertIn(ATTACHED_MARKER, row_a[0],
                      "doc A (edited most recently) must carry the marker")
        self.assertNotIn(ATTACHED_MARKER, row_b[0],
                         "doc B (created later, but edited less recently) must not")


# ---------------------------------------------------------------------------
# 4. "Draft with AI" asks before overwriting
# ---------------------------------------------------------------------------

class DraftWithAIConfirmTests(_LoggedInTestCase):
    """Item 4 — the 'Draft with AI' submit button confirms before overwriting
    whatever is currently in the composer."""

    def test_draft_with_ai_button_confirms_before_overwriting(self):
        d = _dispute()
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        html = resp.content.decode()
        btn = _find_button(html, name='action', value='generate')
        self.assertIsNotNone(btn, 'expected the "Draft with AI" submit button (action=generate)')
        self.assertIn('onclick', btn.lower(), f"missing onclick confirm: {btn}")
        self.assertIn('confirm(', btn, f"missing onclick confirm: {btn}")


# ---------------------------------------------------------------------------
# 5. Enter key must save, not regenerate
# ---------------------------------------------------------------------------

class ComposerEnterKeySavesTests(_LoggedInTestCase):
    """Item 5 — the FIRST submit button inside the composer form must be Save,
    so pressing Enter in a text field submits Save, not 'Draft with AI'."""

    def test_first_submit_button_in_composer_form_is_save(self):
        d = _dispute()
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        html = resp.content.decode()
        start = html.index('id="dispute-composer"')
        end = html.index('</form>', start)
        composer_html = html[start:end]
        m = re.search(r'<button\b[^>]*\btype=["\']submit["\'][^>]*>', composer_html, re.IGNORECASE)
        self.assertIsNotNone(m, "expected a submit button inside the composer form")
        first_submit_button = m.group(0)
        self.assertIn(
            'value="save"', first_submit_button,
            f"the FIRST submit button in the composer must be Save (so Enter saves, not "
            f"regenerate); got: {first_submit_button}")


# ---------------------------------------------------------------------------
# 6. A rejected PayPal send leaves an editable draft
# ---------------------------------------------------------------------------

def _fake_paypal_rejection(submission, performed_by=None):
    """Mirrors what the real submit_dispute_response does on a clean PayPal
    rejection: mark the submission FAILED, persist it, report failure."""
    submission.status = DisputeSubmission.STATUS_FAILED
    submission.save(update_fields=['status', 'updated_at'])
    return False


class RejectedSendKeepsEditableDraftTests(_LoggedInTestCase):
    """Item 6 — a rejection (ok=False, no exception) must not strand the
    manager: the FAILED submission stays as an audit record, and a fresh
    DRAFT is created with the identical text/flags/images."""

    def _post_send(self, dispute, image=None, side_effect=_fake_paypal_rejection):
        payload = {
            'action': 'send',
            'notes': 'Our full narrative for PayPal.',
            'manager_note': 'Stress the IP match.',
            'evidence_type': 'PROOF_OF_FULFILLMENT',
            'attach_evidence_pdf': 'on',
            'attach_terms': 'on',
            'attach_invoice': 'on',
        }
        if image is not None:
            payload['images'] = image
        with patch.object(fv, 'submit_dispute_response', side_effect=side_effect) as submit:
            resp = self.web.post(
                reverse('disputes:dispute_prepare_submission', args=[dispute.id]), payload)
            submit.assert_called_once()
        return resp

    def test_failed_submission_kept_and_new_editable_draft_created(self):
        dispute = _evidence_open_dispute()
        resp = self._post_send(dispute)
        self.assertEqual(resp.status_code, 302)

        failed_qs = dispute.submissions.filter(status=DisputeSubmission.STATUS_FAILED)
        self.assertEqual(failed_qs.count(), 1,
                         "the rejected submission must be kept for the audit trail")
        old = failed_qs.get()

        new_draft = dispute.submissions.filter(status=DisputeSubmission.STATUS_DRAFT).first()
        self.assertIsNotNone(
            new_draft, "a fresh editable DRAFT must be created after a PayPal rejection")
        self.assertNotEqual(new_draft.pk, old.pk)
        self.assertEqual(new_draft.notes, old.notes)
        self.assertEqual(new_draft.manager_note, old.manager_note)
        self.assertEqual(new_draft.evidence_type, old.evidence_type)
        self.assertEqual(new_draft.attach_evidence_pdf, old.attach_evidence_pdf)
        self.assertEqual(new_draft.attach_terms, old.attach_terms)
        self.assertEqual(new_draft.attach_invoice, old.attach_invoice)

    def test_images_carried_over_to_the_new_draft(self):
        dispute = _evidence_open_dispute()
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (20, 20), 'white').save(buf, format='PNG')
        png = SimpleUploadedFile('shot.png', buf.getvalue(), content_type='image/png')
        self._post_send(dispute, image=png)

        old = dispute.submissions.filter(status=DisputeSubmission.STATUS_FAILED).first()
        self.assertIsNotNone(old)
        new_draft = dispute.submissions.filter(status=DisputeSubmission.STATUS_DRAFT).first()
        self.assertIsNotNone(
            new_draft, "a fresh editable DRAFT must be created after a PayPal rejection")
        self.assertEqual(old.images.count(), 1)
        self.assertEqual(
            new_draft.images.count(), 1,
            "the manager's attached image must be re-attached to the fresh draft")
        self.assertEqual(new_draft.images.get().filename, old.images.get().filename)

    def test_followup_get_shows_notes_in_composer_and_error_flash(self):
        dispute = _evidence_open_dispute()
        self._post_send(dispute)
        resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        html = resp.content.decode()
        start = html.index('id="composer-notes"')
        end = html.index('</textarea>', start)
        self.assertIn(
            'Our full narrative for PayPal.', html[start:end],
            "the fresh draft's notes must repopulate the composer so nothing is lost")
        msgs = list(resp.context['messages'])
        self.assertTrue(
            any(m.level_tag == 'error' for m in msgs),
            f"expected an ERROR-level flash message; got: {[(m.level_tag, str(m)) for m in msgs]}")


class RejectionDraftDoesNotLeakIntoExceptionPathTests(_LoggedInTestCase):
    """Guard for the new behaviour above: it must fire ONLY on a clean
    rejection (ok=False), never when submit_dispute_response raises — that
    path already reverts the SAME submission to DRAFT (see
    test_review_fixes.SubmitRaceGuardTests.test_exception_releases_the_claim_for_retry)
    and must still create no duplicate. Expected to PASS already (nothing
    about the exception path should change); kept as a regression guard for
    whatever implements item 6."""

    def test_exception_reverts_same_submission_without_duplicate(self):
        dispute = _evidence_open_dispute()
        draft = DisputeSubmission.objects.create(
            dispute=dispute, notes='ready to send', source='AI', status='DRAFT')
        with patch.object(fv, 'submit_dispute_response', side_effect=RuntimeError('boom')):
            self.web.post(reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
                          {'action': 'send', 'notes': 'ready to send'})
        self.assertEqual(
            dispute.submissions.count(), 1,
            "an exception during submit must not create a duplicate submission")
        draft.refresh_from_db()
        self.assertEqual(draft.status, DisputeSubmission.STATUS_DRAFT)


# ---------------------------------------------------------------------------
# 7. Honest provenance label on the generated evidence report
# ---------------------------------------------------------------------------

COMMENTS = [
    {'author': {'name': 'Mark Johnson', 'email': 'm@alf.com'}, 'public': False,
     'created_at': '2026-02-03T21:14:00Z', 'body': 'Registration ID: ALF1\nName: Lee Foley',
     'attachments': []},
    {'author': {'name': 'Joe Snyder', 'email': 'j@alf.com'}, 'public': True,
     'created_at': '2026-02-04T10:32:00Z', 'body': 'Dear Lee, an update on your search.',
     'attachments': []},
]


class GeneratedByProvenanceTests(TestCase):
    """Item 7 — generate_evidence_report's generated_by must reflect whether
    the AI narrative grouping actually placed items, instead of being
    hardcoded to MANUAL (document_service.py generate_evidence_report, the
    _persist_document(... generated_by=DisputeDocument.GENERATED_BY_MANUAL ...)
    call). The AI-placements case below is the RED assertion; the None case
    already passes today (for the wrong reason — it's unconditionally
    MANUAL) and is kept here as a pin for the desired behaviour."""

    def _dispute_with_claim(self):
        claim = Claim.objects.create(client_email='b@example.com', client_name='Lee Foley',
                                     alf_claim_id='ALF1', zd_ticket_id='97001')
        return Dispute.objects.create(
            paypal_dispute_id='PP-D-PROV', buyer_email='b@example.com', transaction_id='TX',
            transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc), dispute_reason='UNAUTHORISED',
            claim=claim, zd_ticket_id='97001')

    def test_labeled_ai_when_narrate_evidence_places_items(self):
        d = self._dispute_with_claim()
        placements = {1: {'section': 'INTERACTIONS', 'explanation': 'We kept the customer updated.'}}
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch.object(ds, '_render_to_pdf', return_value=b'%PDF-1.4 fake'), \
             patch.object(ds, '_narrate_evidence', return_value=placements):
            doc = ds.generate_evidence_report(d.id)
        self.assertIsNotNone(doc, "report generation must succeed")
        self.assertEqual(
            doc.generated_by, DisputeDocument.GENERATED_BY_AI,
            "generated_by must read AI when _narrate_evidence placed items")

    def test_labeled_manual_when_narrate_evidence_returns_none(self):
        d = self._dispute_with_claim()
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': COMMENTS}), \
             patch.object(ds, '_render_to_pdf', return_value=b'%PDF-1.4 fake'), \
             patch.object(ds, '_narrate_evidence', return_value=None):
            doc = ds.generate_evidence_report(d.id)
        self.assertIsNotNone(doc, "report generation must succeed")
        self.assertEqual(
            doc.generated_by, DisputeDocument.GENERATED_BY_MANUAL,
            "generated_by must read MANUAL when _narrate_evidence found no AI placements")


# ---------------------------------------------------------------------------
# 9. Documents table shows edits, and drops Edit once SENT
# ---------------------------------------------------------------------------

class DocumentRowEditMarkerTests(_LoggedInTestCase):
    """Item 9 — a document's row shows 'edited' + its updated_at once it was
    touched well after creation, and drops the Edit link once SENT."""

    def _detail_html(self, dispute):
        resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_freshly_created_document_row_has_no_edited_marker(self):
        d = _dispute()
        _report_doc(d, version=1)
        html = self._detail_html(d)
        rows = _rows_matching(html, '>v1<')
        self.assertEqual(len(rows), 1)
        self.assertNotIn('edited', rows[0].lower())

    def test_row_shows_edited_and_its_date_when_updated_well_after_created(self):
        d = _dispute()
        doc = _report_doc(d, version=2)
        _touch(doc, updated_at=doc.created_at + timedelta(seconds=30))

        html = self._detail_html(d)
        rows = _rows_matching(html, '>v2<')
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIn('edited', row.lower(),
                      "a document updated 30s after creation must show as edited")
        expected_date = date_filter(doc.updated_at, "M d, Y H:i")
        self.assertIn(expected_date, row,
                      f"expected the updated_at date {expected_date!r} in the row")

    def test_sent_document_has_no_edit_link(self):
        d = _dispute()
        doc = DisputeDocument.objects.create(
            dispute=d, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
            status=DisputeDocument.STATUS_SENT, generated_by='MANUAL', version=3)
        edit_url = reverse('disputes:dispute_edit_document', args=[doc.id])
        html = self._detail_html(d)
        rows = _rows_matching(html, '>v3<')
        self.assertEqual(len(rows), 1)
        self.assertNotIn(edit_url, rows[0], "a SENT document must not offer an Edit link")

    def test_draft_document_still_has_edit_link(self):
        d = _dispute()
        doc = DisputeDocument.objects.create(
            dispute=d, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
            status=DisputeDocument.STATUS_DRAFT, generated_by='MANUAL', version=4)
        edit_url = reverse('disputes:dispute_edit_document', args=[doc.id])
        html = self._detail_html(d)
        rows = _rows_matching(html, '>v4<')
        self.assertEqual(len(rows), 1)
        self.assertIn(edit_url, rows[0], "a DRAFT document must still offer an Edit link")
