"""Red-phase spec — dispute-reply editing safety, round 2 (deliberately
unimplemented; these tests must fail until the behaviour lands):

1. Honest AI provenance for the image-only path: when the case record is made
   ENTIRELY of image-only internal notes, _narrate_evidence never runs (there
   is no text to classify), and generated_by must still read AI when vision
   placed items — not unconditionally MANUAL just because the TEXT path never
   fired.
2. A crash while cloning a rejected draft's images must not surface as a 500:
   the manager still gets a redirect + an error flash, the FAILED row stays,
   and no half-cloned DRAFT (carrying only some of the original images) is
   left behind.
3. The generate-report confirm is scoped to EVIDENCE REPORTS specifically — a
   stray legacy RESPONSE_LETTER document must not trigger it, but an
   EVIDENCE_REPORT row (even with no file yet) must.
4. The "Attached to the next reply" badge never appears for a report that has
   no file yet.
5. A working draft already claimed (SUBMITTING) must not be invisible to a
   fresh 'send' — no silent duplicate submission.
6. _narrative_untrusted must respect an explicit, small max_comments cap, not
   just its (round-1) new default of 40.
7. (Regression pin, expected to already pass) the generate-confirm and
   redraft-confirm text carry no em-dash.
"""

import re
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, TestCase
from django.urls import reverse

from apps.claims.models import Claim
from apps.payments import document_service as ds
from apps.payments import frontend_views as fv
from apps.payments.models import Dispute, DisputeDocument, DisputeSubmission, DisputeSubmissionImage

User = get_user_model()

ATTACHED_MARKER = 'Attached to the next reply'


def _dispute(payload=None, **kw):
    base = dict(paypal_dispute_id='PP-D-EDITSAFE2', buyer_email='b@example.com',
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


def _fake_paypal_rejection(submission, performed_by=None):
    """Mirrors what the real submit_dispute_response does on a clean PayPal
    rejection: mark the submission FAILED, persist it, report failure."""
    submission.status = DisputeSubmission.STATUS_FAILED
    submission.save(update_fields=['status', 'updated_at'])
    return False


def _bundle(n):
    """The minimal structure _narrative_untrusted needs: bundle['panels'], an
    ORDERED list of dicts with 'body'/'public' (mirrors
    test_narrative_input_window.py's helper)."""
    return {'panels': [{'body': f'RECORD-{i:02d} case note text.', 'public': bool(i % 2)}
                        for i in range(1, n + 1)]}


class _LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='editsafe2_mgr', password='x')
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


# ---------------------------------------------------------------------------
# 1. Honest provenance for the image-only path (vision-only classification)
# ---------------------------------------------------------------------------

class ImageOnlyProvenanceTests(TestCase):
    """Item 1 — when the case record is made ENTIRELY of image-only internal
    notes (no text records at all), text_items is empty so _narrate_evidence
    is never called; generated_by must still honestly read AI when
    _narrate_image_evidence (vision) placed items, and MANUAL when neither
    narrator placed anything. Mirrors round-1's GeneratedByProvenanceTests
    patching pattern, but for the vision-only path."""

    IMAGE_ONLY_COMMENTS = [
        {'author': {'name': 'Mark Johnson', 'email': 'm@alf.com'}, 'public': False,
         'created_at': '2026-02-03T21:14:00Z', 'body': 'FRONTIER',
         'attachments': [{'content_type': 'image/png', 'content_url': 'https://zd/f.png',
                          'file_name': 'f.png'}]},
    ]

    def _dispute_with_claim(self):
        claim = Claim.objects.create(client_email='b@example.com', client_name='Lee Foley',
                                     alf_claim_id='ALF1', zd_ticket_id='97001')
        return Dispute.objects.create(
            paypal_dispute_id='PP-D-IMGPROV', buyer_email='b@example.com', transaction_id='TX',
            transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc), dispute_reason='UNAUTHORISED',
            claim=claim, zd_ticket_id='97001')

    def test_labeled_ai_when_image_only_case_and_vision_places_items(self):
        d = self._dispute_with_claim()
        placements = {1: {'section': 'SUBMISSIONS', 'explanation': 'We reported the loss to Frontier.'}}
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': self.IMAGE_ONLY_COMMENTS}), \
             patch.object(ds, '_attachment_data_uri', return_value='data:image/png;base64,AAAA'), \
             patch.object(ds, '_render_to_pdf', return_value=b'%PDF-1.4 fake'), \
             patch.object(ds, '_narrate_evidence') as narrate_evidence, \
             patch.object(ds, '_narrate_image_evidence', return_value=placements):
            doc = ds.generate_evidence_report(d.id)
        narrate_evidence.assert_not_called()
        self.assertIsNotNone(doc, "report generation must succeed")
        self.assertEqual(
            doc.generated_by, DisputeDocument.GENERATED_BY_AI,
            "generated_by must read AI when vision placed the image-only records, even though "
            "the text classifier never ran (there was no text to classify)")

    def test_labeled_manual_when_vision_places_nothing(self):
        d = self._dispute_with_claim()
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': self.IMAGE_ONLY_COMMENTS}), \
             patch.object(ds, '_attachment_data_uri', return_value='data:image/png;base64,AAAA'), \
             patch.object(ds, '_render_to_pdf', return_value=b'%PDF-1.4 fake'), \
             patch.object(ds, '_narrate_evidence') as narrate_evidence, \
             patch.object(ds, '_narrate_image_evidence', return_value=None):
            doc = ds.generate_evidence_report(d.id)
        narrate_evidence.assert_not_called()
        self.assertIsNotNone(doc, "report generation must succeed")
        self.assertEqual(
            doc.generated_by, DisputeDocument.GENERATED_BY_MANUAL,
            "generated_by must read MANUAL when neither narrator placed anything")


# ---------------------------------------------------------------------------
# 2. A crash while cloning a rejected draft's images must not 500
# ---------------------------------------------------------------------------

class RejectedSendCloneFailureTests(_LoggedInTestCase):
    """Item 2 — the image-clone loop that follows a clean PayPal rejection has
    no error handling today; if copying an image onto the fresh draft blows
    up (e.g. a storage hiccup), that must not take the whole request down
    with it."""

    def test_clone_image_failure_does_not_crash_and_leaves_no_partial_draft(self):
        dispute = _evidence_open_dispute()
        draft = DisputeSubmission.objects.create(
            dispute=dispute, notes='Our full narrative for PayPal.',
            source=DisputeSubmission.SOURCE_MANUAL, status=DisputeSubmission.STATUS_DRAFT)
        DisputeSubmissionImage.objects.create(submission=draft, file='shot.png', uploaded_by=self.user)
        self.assertEqual(draft.images.count(), 1)  # sanity: the failed draft has one image

        with patch.object(fv, 'submit_dispute_response', side_effect=_fake_paypal_rejection), \
             patch.object(DisputeSubmissionImage.objects, 'create', side_effect=Exception('boom')):
            try:
                resp = self.web.post(
                    reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
                    {'action': 'send', 'notes': 'Our full narrative for PayPal.'})
            except Exception as e:
                self.fail(
                    "a failure while cloning the rejected draft's images must not crash the "
                    f"request (no 500) — got an unhandled {e!r}")

        self.assertEqual(resp.status_code, 302, "must redirect, not 500, on a clone failure")

        failed_qs = dispute.submissions.filter(status=DisputeSubmission.STATUS_FAILED)
        self.assertEqual(failed_qs.count(), 1,
                         "the rejected submission must still be kept as an audit record")

        for d in dispute.submissions.filter(status=DisputeSubmission.STATUS_DRAFT):
            self.assertEqual(
                d.images.count(), 1,
                "a cloned DRAFT must carry ALL of the original images, never a partial set "
                f"(draft #{d.pk} has {d.images.count()} of 1)")

        resp2 = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        msgs = list(resp2.context['messages'])
        self.assertTrue(
            any(m.level_tag == 'error' for m in msgs),
            f"expected an ERROR-level flash message; got: {[(m.level_tag, str(m)) for m in msgs]}")


# ---------------------------------------------------------------------------
# 3. Generate confirmation is scoped to EVIDENCE REPORTS specifically
# ---------------------------------------------------------------------------

class GenerateConfirmationScopedToReportsTests(_LoggedInTestCase):
    """Item 3 — the generate-documents confirm exists to warn about
    overwriting an EVIDENCE REPORT; a stray legacy RESPONSE_LETTER document
    must not trigger it, but an EVIDENCE_REPORT row (even with no file yet)
    must."""

    def _generate_forms(self, dispute):
        action_url = reverse('disputes:dispute_generate_documents', args=[dispute.id])
        resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        forms = _forms_with_action(resp.content.decode(), action_url)
        self.assertGreaterEqual(
            len(forms), 2,
            "expected the generate-documents form on both the evidence-report card and the sidebar")
        return forms

    def test_legacy_response_letter_only_does_not_confirm(self):
        d = _dispute()
        DisputeDocument.objects.create(
            dispute=d, doc_type=DisputeDocument.DOC_TYPE_RESPONSE_LETTER,
            status=DisputeDocument.STATUS_DRAFT, generated_by='MANUAL', version=1)
        for tag in self._generate_forms(d):
            self.assertNotIn(
                'confirm(', tag,
                f"a legacy response-letter document must not trigger the evidence-report "
                f"overwrite confirm: {tag}")

    def test_evidence_report_without_file_still_confirms_on_both_forms(self):
        d = _dispute()
        DisputeDocument.objects.create(
            dispute=d, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
            status=DisputeDocument.STATUS_DRAFT, generated_by='MANUAL', version=1)
        for tag in self._generate_forms(d):
            self.assertIn('onsubmit', tag.lower(), f"missing onsubmit confirm: {tag}")
            self.assertIn(
                'confirm(', tag,
                f"an EVIDENCE_REPORT row (even without a file) must trigger the confirm: {tag}")


# ---------------------------------------------------------------------------
# 4. The attached-next-reply badge requires a real file
# ---------------------------------------------------------------------------

class AttachedBadgeRequiresFileTests(_LoggedInTestCase):
    """Item 4 — the 'Attached to the next reply' badge must never appear for
    an EVIDENCE_REPORT row that has no file (nothing could actually attach to
    the next reply)."""

    def test_no_badge_when_only_report_has_no_file(self):
        d = _dispute()
        DisputeDocument.objects.create(
            dispute=d, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
            status=DisputeDocument.STATUS_DRAFT, generated_by='MANUAL', version=1)
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        html = resp.content.decode()
        self.assertNotIn(ATTACHED_MARKER, html,
                         "a fileless EVIDENCE_REPORT must never show the 'attached' badge")


# ---------------------------------------------------------------------------
# 5. Double-submit safety when the draft is already SUBMITTING
# ---------------------------------------------------------------------------

class DoubleSubmitSafetyTests(_LoggedInTestCase):
    """Item 5 — _working_draft only ever looks for STATUS_DRAFT rows, so a
    submission already claimed (SUBMITTING) is invisible to it. Posting
    action=send while one is in flight must surface the existing "already
    being sent" error, create no additional DRAFT, and never call
    submit_dispute_response a second time."""

    def test_send_while_submitting_does_not_clone_or_resubmit(self):
        dispute = _evidence_open_dispute()
        inflight = DisputeSubmission.objects.create(
            dispute=dispute, notes='Already going out.', source=DisputeSubmission.SOURCE_MANUAL,
            status=DisputeSubmission.STATUS_SUBMITTING)

        with patch.object(fv, 'submit_dispute_response') as submit:
            resp = self.web.post(
                reverse('disputes:dispute_prepare_submission', args=[dispute.id]),
                {'action': 'send', 'notes': 'Trying to send again.'})
            submit.assert_not_called()

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            dispute.submissions.filter(status=DisputeSubmission.STATUS_DRAFT).count(), 0,
            "no new DRAFT may be created while a submission is already in flight (SUBMITTING)")
        inflight.refresh_from_db()
        self.assertEqual(inflight.status, DisputeSubmission.STATUS_SUBMITTING,
                         "the in-flight submission must be left completely alone")

        resp2 = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        msgs = [str(m) for m in resp2.context['messages']]
        self.assertTrue(
            any('already being sent' in m for m in msgs),
            f"expected the existing 'already being sent' error message; got: {msgs}")


# ---------------------------------------------------------------------------
# 6. _narrative_untrusted must respect an explicit small cap
# ---------------------------------------------------------------------------

class NarrativeRecordCapParameterTests(TestCase):
    """Item 6 — _narrative_untrusted must respect an explicit small
    max_comments cap, not just its (round-1) new default of 40. The
    'earliest 8' block must shrink when the cap itself is under 8."""

    def test_cap_of_5_keeps_exactly_5_earliest_records(self):
        bundle = _bundle(30)
        out = ds._narrative_untrusted(bundle, max_comments=5)
        kept = out.get('zendesk_comment', [])
        self.assertEqual(len(kept), 5,
                         f"expected exactly 5 records kept under max_comments=5; got {len(kept)}")
        rendered = '\n'.join(kept)
        for i in range(1, 6):
            self.assertIn(f'RECORD-{i:02d}', rendered,
                         f"RECORD-{i:02d} is one of the earliest 5 and must be kept")
        for i in range(6, 31):
            self.assertNotIn(f'RECORD-{i:02d}', rendered,
                             f"RECORD-{i:02d} must be dropped under a 5-record cap")

    def test_cap_of_8_keeps_exactly_8_records(self):
        bundle = _bundle(30)
        out = ds._narrative_untrusted(bundle, max_comments=8)
        kept = out.get('zendesk_comment', [])
        self.assertEqual(len(kept), 8,
                         f"expected exactly 8 records kept under max_comments=8; got {len(kept)}")


# ---------------------------------------------------------------------------
# 7. Regression pin — no em-dash in the confirm() prompts
# ---------------------------------------------------------------------------

class NoEmDashInConfirmTextTests(_LoggedInTestCase):
    """Item 7 (regression pin, expected to already pass) — the system-wide
    anti-AI-voice rule (no em-dashes in generated/hand-written user-facing
    text, PR #21's prompt_fence.STYLE_RULE) applies to the hand-written JS
    confirm() prompts on this page too."""

    def test_generate_and_redraft_confirm_text_has_no_em_dash(self):
        d = _dispute()
        _report_doc(d)  # a report exists, so both generate forms carry a confirm
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        html = resp.content.decode()

        generate_action = reverse('disputes:dispute_generate_documents', args=[d.id])
        generate_forms = _forms_with_action(html, generate_action)
        self.assertGreaterEqual(len(generate_forms), 1, "expected at least one generate-documents form")
        for tag in generate_forms:
            self.assertNotIn('—', tag, f"em-dash found in generate-confirm text: {tag}")

        draft_btn = _find_button(html, name='action', value='generate')
        self.assertIsNotNone(draft_btn, 'expected the "Draft with AI" submit button (action=generate)')
        self.assertNotIn('—', draft_btn, f"em-dash found in redraft-confirm text: {draft_btn}")
