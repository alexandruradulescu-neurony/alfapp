"""Phases B + D — the dispute-page UI: prepare/submit/manual-reply views and
the reply-timeline assembly."""

from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from django.urls import reverse

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments import document_service as ds
from apps.payments import frontend_views as fv
from apps.payments.models import (Dispute, DisputeSubmission, DisputeSubmissionImage)

User = get_user_model()


def _dispute(payload=None, **kw):
    base = dict(paypal_dispute_id='PP-D-UI', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='UNAUTHORISED', status='MATCHED',
                raw_webhook_payload=payload or {})
    base.update(kw)
    return Dispute.objects.create(**base)


class TimelineBuilderTests(TestCase):
    def test_merges_submission_evidence_and_message_chronologically(self):
        d = _dispute(payload={
            'evidences': [{'evidence_type': 'PROOF_OF_FULFILLMENT', 'notes': 'on file',
                           'source': 'SUBMITTED_BY_SELLER', 'date': '2026-06-02T10:00:00Z'}],
            'messages': [{'posted_by': 'BUYER', 'content': 'I want a refund',
                          'time_posted': '2026-06-01T09:00:00Z'}],
        })
        DisputeSubmission.objects.create(dispute=d, notes='our case', status='SUBMITTED',
                                         kind='EVIDENCE',
                                         submitted_at=datetime(2026, 6, 3, tzinfo=dt_tz.utc))
        tl = ds.build_dispute_reply_timeline(d)
        self.assertEqual(len(tl), 3)
        actors = [e['actor'] for e in tl]
        # chronological: buyer message (Jun 1) → PayPal evidence (Jun 2) → our submission (Jun 3)
        self.assertEqual(actors, ['Buyer', 'Airport Lost & Found', 'Airport Lost & Found'])
        self.assertEqual(tl[0]['kind'], 'paypal_message')
        self.assertEqual(tl[2]['kind'], 'submission')
        self.assertEqual(tl[2]['status'], 'SUBMITTED')

    def test_requested_from_seller_is_attributed_to_paypal(self):
        d = _dispute(payload={'evidences': [
            {'evidence_type': 'OTHER', 'notes': 'send proof', 'source': 'REQUESTED_FROM_SELLER'}]})
        tl = ds.build_dispute_reply_timeline(d)
        self.assertEqual(tl[0]['actor'], 'PayPal')
        self.assertIn('requested', tl[0]['title'].lower())

    def test_empty_when_nothing(self):
        self.assertEqual(ds.build_dispute_reply_timeline(_dispute()), [])


class _UITestBase(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='ui_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)


class DetailRenderTests(_UITestBase):
    def test_detail_shows_prepare_panel_and_timeline(self):
        d = _dispute()
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Prepare submission to PayPal')
        self.assertContains(resp, 'Reply timeline')


class PrepareSubmissionTests(_UITestBase):
    def test_generate_creates_draft_with_fallback_when_ai_off(self):
        ss = SystemSettings.get_instance()
        ss.ai_api_key = ''
        ss.save()
        claim = Claim.objects.create(client_email='b@example.com', client_name='Lee Foley',
                                     alf_claim_id='ALF1', zd_ticket_id='97001')
        d = _dispute(claim=claim, zd_ticket_id='97001')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {}, 'comments': []}):
            resp = self.web.post(reverse('disputes:dispute_prepare_submission', args=[d.id]),
                                 {'action': 'generate', 'manager_note': 'stress the IP'})
        self.assertEqual(resp.status_code, 302)
        draft = d.submissions.get()
        self.assertEqual(draft.status, 'DRAFT')
        self.assertEqual(draft.source, 'AI')
        self.assertEqual(draft.manager_note, 'stress the IP')
        self.assertIn('Lee Foley', draft.notes)
        self.assertEqual(draft.evidence_type, 'PROOF_OF_FULFILLMENT')  # defaulted from reason

    def test_save_marks_ai_edited_and_attaches_image(self):
        d = _dispute()
        DisputeSubmission.objects.create(dispute=d, notes='AI text', source='AI', status='DRAFT')
        import io
        from PIL import Image
        _b = io.BytesIO(); Image.new('RGB', (40, 40), 'white').save(_b, format='PNG')
        png = SimpleUploadedFile('shot.png', _b.getvalue(), content_type='image/png')
        resp = self.web.post(reverse('disputes:dispute_prepare_submission', args=[d.id]), {
            'action': 'save', 'notes': 'AI text, now edited by hand',
            'manager_note': 'note', 'attach_evidence_pdf': 'on', 'evidence_type': 'PROOF_OF_FULFILLMENT',
            'images': png,
        })
        self.assertEqual(resp.status_code, 302)
        draft = d.submissions.get()
        self.assertEqual(draft.source, 'AI_EDITED')      # text changed from the AI draft
        self.assertTrue(draft.attach_evidence_pdf)
        self.assertEqual(draft.notes, 'AI text, now edited by hand')
        self.assertEqual(draft.images.count(), 1)

    def test_save_ignores_non_image_upload(self):
        d = _dispute()
        DisputeSubmission.objects.create(dispute=d, notes='x', source='MANUAL', status='DRAFT')
        bad = SimpleUploadedFile('a.exe', b'MZ', content_type='application/octet-stream')
        self.web.post(reverse('disputes:dispute_prepare_submission', args=[d.id]),
                      {'action': 'save', 'notes': 'x', 'images': bad})
        self.assertEqual(DisputeSubmissionImage.objects.count(), 0)


class SubmitToPayPalTests(_UITestBase):
    def test_submit_calls_orchestration_on_success(self):
        d = _dispute(payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        DisputeSubmission.objects.create(dispute=d, notes='ready', source='AI', status='DRAFT')
        with patch.object(fv, 'submit_dispute_response', return_value=True) as submit:
            resp = self.web.post(reverse('disputes:dispute_submit_to_paypal', args=[d.id]), follow=True)
            submit.assert_called_once()
        self.assertContains(resp, 'Submitted to PayPal')

    def test_submit_blocked_without_draft(self):
        d = _dispute(payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        with patch.object(fv, 'submit_dispute_response') as submit:
            resp = self.web.post(reverse('disputes:dispute_submit_to_paypal', args=[d.id]), follow=True)
            submit.assert_not_called()
        self.assertContains(resp, 'Prepare a submission first')

    def test_submit_blocked_without_endpoint(self):
        d = _dispute(status='RESOLVED_WON', payload={'status': 'RESOLVED'})
        DisputeSubmission.objects.create(dispute=d, notes='ready', status='DRAFT')
        with patch.object(fv, 'submit_dispute_response') as submit:
            resp = self.web.post(reverse('disputes:dispute_submit_to_paypal', args=[d.id]), follow=True)
            submit.assert_not_called()
        self.assertContains(resp, "isn't accepting a submission")


class ManualReplyTests(_UITestBase):
    def test_manual_reply_creates_submission_and_submits(self):
        d = _dispute(payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        with patch.object(fv, 'submit_dispute_response', return_value=True) as submit:
            resp = self.web.post(reverse('disputes:dispute_manual_reply', args=[d.id]),
                                 {'reply_text': 'Adding more context.'}, follow=True)
            submit.assert_called_once()
        sub = d.submissions.get()
        self.assertEqual(sub.notes, 'Adding more context.')
        self.assertEqual(sub.source, 'MANUAL')
        self.assertContains(resp, 'Reply sent to PayPal')

    def test_manual_reply_requires_text(self):
        d = _dispute(payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        with patch.object(fv, 'submit_dispute_response') as submit:
            self.web.post(reverse('disputes:dispute_manual_reply', args=[d.id]), {'reply_text': '  '})
            submit.assert_not_called()
        self.assertEqual(d.submissions.count(), 0)


class DeleteImageTests(_UITestBase):
    def test_delete_image_from_draft(self):
        d = _dispute()
        sub = DisputeSubmission.objects.create(dispute=d, notes='x', status='DRAFT')
        img = DisputeSubmissionImage.objects.create(submission=sub)
        img.file.save('s.png', SimpleUploadedFile('s.png', b'P', content_type='image/png'), save=True)
        resp = self.web.post(reverse('disputes:dispute_delete_submission_image', args=[img.id]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sub.images.count(), 0)

    def test_cannot_delete_image_from_submitted(self):
        d = _dispute()
        sub = DisputeSubmission.objects.create(dispute=d, notes='x', status='SUBMITTED')
        img = DisputeSubmissionImage.objects.create(submission=sub)
        img.file.save('s.png', SimpleUploadedFile('s.png', b'P', content_type='image/png'), save=True)
        self.web.post(reverse('disputes:dispute_delete_submission_image', args=[img.id]))
        self.assertEqual(sub.images.count(), 1)  # untouched
