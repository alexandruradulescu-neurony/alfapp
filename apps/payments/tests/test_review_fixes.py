"""Fixes from the Codex code review (verified findings):
H1 submit race, H2 manual-reply channel gate, H3 accept-claim race, M4 refund
endpoint, M5 image-upload validation, M6 manual-link txn warning, M7 editor XSS."""

from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from django.urls import reverse

from apps.claims.models import Claim
from apps.payments import frontend_views as fv
from apps.payments.models import (Dispute, DisputeDocument, DisputeSubmission,
                                  DisputeSubmissionImage)

User = get_user_model()


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-RF', buyer_email='b@e.com', transaction_id='TX',
                transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED', raw_webhook_payload={})
    base.update(kw)
    return Dispute.objects.create(**base)


class _Base(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='rf_mgr', password='x', role='MANAGER')
        self.web = Client()
        self.web.force_login(self.mgr)


class SubmitRaceGuardTests(_Base):
    """H1 — the draft is atomically claimed (DRAFT->SUBMITTING) before the POST."""

    def _draft(self):
        d = _dispute(raw_webhook_payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        sub = DisputeSubmission.objects.create(dispute=d, notes='ready', source='AI', status='DRAFT')
        return d, sub

    def test_claims_draft_then_submits_once(self):
        d, sub = self._draft()
        with patch.object(fv, 'submit_dispute_response', return_value=True) as submit:
            self.web.post(reverse('disputes:dispute_submit_to_paypal', args=[d.id]))
            submit.assert_called_once()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'SUBMITTING')  # claim ran (mock didn't finalize)

    def test_in_flight_submission_is_not_resubmittable(self):
        d, sub = self._draft()
        sub.status = 'SUBMITTING'
        sub.save(update_fields=['status'])
        with patch.object(fv, 'submit_dispute_response') as submit:
            self.web.post(reverse('disputes:dispute_submit_to_paypal', args=[d.id]))
            submit.assert_not_called()       # no DRAFT to claim

    def test_exception_releases_the_claim_for_retry(self):
        d, sub = self._draft()
        with patch.object(fv, 'submit_dispute_response', side_effect=RuntimeError('boom')):
            self.web.post(reverse('disputes:dispute_submit_to_paypal', args=[d.id]))
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'DRAFT')  # reset so the manager can retry


class ManualReplyGateTests(_Base):
    """H2 — the quick reply is the follow-up channel only, enforced server-side."""

    def test_rejected_in_first_response_window(self):
        d = _dispute(raw_webhook_payload={'dispute_state': 'REQUIRED_ACTION'},
                     dispute_life_cycle_stage='CHARGEBACK')  # submit_endpoint == provide-evidence
        with patch.object(fv, 'submit_dispute_response') as submit:
            resp = self.web.post(reverse('disputes:dispute_manual_reply', args=[d.id]),
                                 {'reply_text': 'hi'}, follow=True)
            submit.assert_not_called()
        self.assertEqual(d.submissions.count(), 0)
        self.assertContains(resp, "under PayPal review yet")

    def test_allowed_under_review(self):
        d = _dispute(raw_webhook_payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        with patch.object(fv, 'submit_dispute_response', return_value=True) as submit:
            self.web.post(reverse('disputes:dispute_manual_reply', args=[d.id]), {'reply_text': 'hi'})
            submit.assert_called_once()
        self.assertEqual(d.submissions.count(), 1)


class AcceptClaimRaceTests(_Base):
    """H3 — the money-moving accept is claimed via accept_in_flight, released after."""

    def test_accept_sets_and_releases_flag(self):
        d = _dispute(status='MATCHED')
        with patch.object(fv, 'accept_claim', return_value=True) as ac:
            self.web.post(reverse('disputes:dispute_accept_claim', args=[d.id]), {'note': 'x'})
            ac.assert_called_once()
        d.refresh_from_db()
        self.assertFalse(d.accept_in_flight)

    def test_concurrent_accept_is_rejected(self):
        d = _dispute(status='MATCHED', accept_in_flight=True)  # already in flight
        with patch.object(fv, 'accept_claim') as ac:
            resp = self.web.post(reverse('disputes:dispute_accept_claim', args=[d.id]), follow=True)
            ac.assert_not_called()
        self.assertContains(resp, 'already being processed')

    def test_flag_released_on_exception(self):
        d = _dispute(status='MATCHED')
        with patch.object(fv, 'accept_claim', side_effect=RuntimeError('boom')):
            self.web.post(reverse('disputes:dispute_accept_claim', args=[d.id]))
        d.refresh_from_db()
        self.assertFalse(d.accept_in_flight)


class ImageUploadValidationTests(_Base):
    """M5 — server-side validation; the client content_type is not trusted."""

    def _draft_dispute(self):
        d = _dispute()
        DisputeSubmission.objects.create(dispute=d, notes='x', source='MANUAL', status='DRAFT')
        return d

    def test_spoofed_content_type_rejected_by_extension(self):
        d = self._draft_dispute()
        bad = SimpleUploadedFile('notreally.txt', b'data', content_type='image/png')  # spoofed
        self.web.post(reverse('disputes:dispute_prepare_submission', args=[d.id]),
                      {'action': 'save', 'notes': 'x', 'images': bad})
        self.assertEqual(DisputeSubmissionImage.objects.count(), 0)

    def test_valid_png_accepted(self):
        d = self._draft_dispute()
        png = SimpleUploadedFile('shot.png', b'PNGDATA', content_type='image/png')
        self.web.post(reverse('disputes:dispute_prepare_submission', args=[d.id]),
                      {'action': 'save', 'notes': 'x', 'images': png})
        self.assertEqual(DisputeSubmissionImage.objects.count(), 1)


class ManualLinkTxnWarningTests(_Base):
    """M6 — manual link is allowed (human override) but warns on a txn mismatch."""

    def test_warns_on_txn_mismatch(self):
        claim = Claim.objects.create(client_email='a@b.com', alf_claim_id='ALFX',
                                     paypal_transaction_id='AAA')
        d = _dispute(claim=None, transaction_id='BBB', status='RECEIVED')
        resp = self.web.post(reverse('disputes:dispute_link_claim', args=[d.id]),
                             {'claim_ref': 'ALFX'}, follow=True)
        d.refresh_from_db()
        self.assertEqual(d.claim_id, claim.id)          # still linked
        self.assertContains(resp, 'transaction IDs differ')

    def test_no_warning_when_txn_matches(self):
        Claim.objects.create(client_email='a@b.com', alf_claim_id='ALFY',
                             paypal_transaction_id='SAME')
        d = _dispute(claim=None, transaction_id='SAME', status='RECEIVED')
        resp = self.web.post(reverse('disputes:dispute_link_claim', args=[d.id]),
                             {'claim_ref': 'ALFY'}, follow=True)
        self.assertNotContains(resp, 'transaction IDs differ')


class EditDocSanitizeTests(_Base):
    """M7 — edited HTML is stored sanitised (the editor re-renders it in srcdoc)."""

    def test_script_stripped_on_save(self):
        d = _dispute()
        doc = DisputeDocument.objects.create(dispute=d, doc_type='RESPONSE_LETTER',
                                             status='DRAFT', generated_by='AI',
                                             content_html='x', version=1)
        self.web.post(reverse('disputes:dispute_edit_document', args=[doc.id]),
                      {'content_html': '<p>hi</p><script>evil()</script>', 'version_increment': 'on'})
        doc.refresh_from_db()
        self.assertIn('<p>hi</p>', doc.content_html)
        self.assertNotIn('<script>', doc.content_html)


class RefundModalEndpointTests(_Base):
    """M4 — the manager refund modal targets the working WooCommerce endpoint."""

    def test_modal_posts_to_issue_not_process(self):
        resp = self.web.get('/manager/refunds/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '/api/payments/refunds/issue/')
        self.assertNotContains(resp, '/api/payments/refunds/process/')
