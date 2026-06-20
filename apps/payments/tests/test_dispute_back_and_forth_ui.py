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

    def test_buyer_submitted_evidence_is_never_attributed_to_us(self):
        """Regression (Angelina Solano dispute): PayPal records the buyer's
        opening complaint BOTH as a SUBMITTED_BY_BUYER/CREATE evidence and as a
        buyer message — same text, same time. The evidence must be attributed to
        the Buyer (never 'Airport Lost & Found'), and the duplicate of the
        buyer's message must not be shown twice."""
        scam = 'This website is a scam but I had already paid them before finding out'
        d = _dispute(payload={
            'evidences': [
                {'evidence_type': 'CREATE', 'notes': scam + ' ',   # trailing space, as PayPal sends
                 'source': 'SUBMITTED_BY_BUYER', 'date': '2026-06-07T20:03:00Z'},
                {'evidence_type': 'PROOF_OF_FULFILLMENT', 'notes': 'our case on file',
                 'source': 'SUBMITTED_BY_SELLER', 'date': '2026-06-09T07:33:00Z'},
            ],
            'messages': [
                {'posted_by': 'BUYER', 'content': scam, 'time_posted': '2026-06-07T20:03:00Z'},
            ],
        })
        tl = ds.build_dispute_reply_timeline(d)
        # The buyer's words are never attributed to us.
        for e in tl:
            if scam[:25] in (e['text'] or ''):
                self.assertEqual(e['actor'], 'Buyer',
                                 f"buyer complaint mislabelled as {e['actor']!r}")
        # And it appears exactly once (the CREATE evidence is deduped against the message).
        shown = sum(1 for e in tl if scam[:25] in (e['text'] or ''))
        self.assertEqual(shown, 1, f'buyer complaint shown {shown}x, expected once')
        # Our own seller-submitted evidence is still attributed to us.
        ours = [e for e in tl if 'our case on file' in (e['text'] or '')]
        self.assertEqual(ours[0]['actor'], 'Airport Lost & Found')

    def test_seller_evidence_reads_as_submitted_and_a_bare_other_request_is_described(self):
        """Our SUBMITTED_BY_SELLER evidence must read as clearly *sent* (not the
        vague 'On file at PayPal') and keep its informative type. A bare 'OTHER'
        PayPal request is no longer dropped as a blank card — it's surfaced as a
        described option ('Other supporting evidence')."""
        d = _dispute(payload={'evidences': [
            {'evidence_type': 'PROOF_OF_FULFILLMENT', 'notes': 'our proof',
             'source': 'SUBMITTED_BY_SELLER', 'date': '2026-06-09T07:33:00Z'},
            {'evidence_type': 'OTHER', 'source': 'REQUESTED_FROM_SELLER',
             'date': '2026-06-09T07:34:00Z'},  # no notes
        ]})
        tl = ds.build_dispute_reply_timeline(d)
        seller = next(e for e in tl if e['actor'] == 'Airport Lost & Found')
        self.assertIn('submitted', seller['title'].lower())          # clearly sent
        self.assertEqual(seller['source'], 'PROOF_OF_FULFILLMENT')   # informative type kept
        paypal = next(e for e in tl if e['actor'] == 'PayPal')
        self.assertIn('requested', paypal['title'].lower())
        self.assertIn('other supporting evidence', paypal['text'].lower())  # not a blank card

    def test_one_request_with_many_types_is_a_single_described_card(self):
        """PayPal lists each acceptable evidence type as its own same-timestamp
        entry — it is ONE request. The page must show a single card listing every
        option with guidance (including 'Other'), plus the accept-by-refund route
        from allowed_response_options. Regression for dispute #138."""
        ts = '2026-06-20T05:53:30.444Z'
        d = _dispute(payload={
            'evidences': [
                {'source': 'REQUESTED_FROM_SELLER', 'evidence_type': 'PROOF_OF_FULFILLMENT', 'date': ts},
                {'source': 'REQUESTED_FROM_SELLER', 'evidence_type': 'PROOF_OF_REFUND', 'date': ts},
                {'source': 'REQUESTED_FROM_SELLER', 'evidence_type': 'OTHER', 'date': ts},
            ],
            'allowed_response_options': {'accept_claim': {'accept_claim_types': ['REFUND']}},
        })
        tl = ds.build_dispute_reply_timeline(d)
        requests = [e for e in tl if e['actor'] == 'PayPal' and 'requested' in e['title'].lower()]
        self.assertEqual(len(requests), 1)                       # ONE card, not three
        body = requests[0]['text'].lower()
        self.assertIn('proof of fulfilment', body)               # all three options described
        self.assertIn('proof of refund', body)
        self.assertIn('other supporting evidence', body)         # OTHER no longer dropped
        self.assertIn('refunding the buyer', body)               # accept-by-refund surfaced

    def test_empty_when_nothing(self):
        self.assertEqual(ds.build_dispute_reply_timeline(_dispute()), [])


class _UITestBase(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='ui_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)


class DetailRenderTests(_UITestBase):
    def test_detail_shows_composer_and_thread(self):
        d = _dispute()
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Your reply to PayPal')        # the unified composer
        self.assertContains(resp, 'Conversation with PayPal')    # the thread
        # A multi-line {# #} once leaked into the page; templating comments must
        # never render as visible text.
        self.assertNotContains(resp, 'Single-column case log')


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

    def test_save_attaches_pdf_document(self):
        d = _dispute()
        DisputeSubmission.objects.create(dispute=d, notes='x', source='MANUAL', status='DRAFT')
        pdf = SimpleUploadedFile('terms.pdf', b'%PDF-1.7\n...', content_type='application/pdf')
        resp = self.web.post(reverse('disputes:dispute_prepare_submission', args=[d.id]),
                             {'action': 'save', 'notes': 'x', 'images': pdf})
        self.assertEqual(resp.status_code, 302)
        draft = d.submissions.get()
        self.assertEqual(draft.images.count(), 1)
        att = draft.images.get()
        self.assertTrue(att.is_pdf)                       # rendered as a PDF chip, not <img>
        self.assertTrue(att.filename.endswith('.pdf'))

    def test_save_attaches_pdf_and_image_together(self):
        d = _dispute()
        DisputeSubmission.objects.create(dispute=d, notes='x', source='MANUAL', status='DRAFT')
        import io
        from PIL import Image
        _b = io.BytesIO(); Image.new('RGB', (10, 10), 'white').save(_b, format='PNG')
        png = SimpleUploadedFile('shot.png', _b.getvalue(), content_type='image/png')
        pdf = SimpleUploadedFile('doc.pdf', b'%PDF-1.4 stuff', content_type='application/pdf')
        self.web.post(reverse('disputes:dispute_prepare_submission', args=[d.id]),
                      {'action': 'save', 'notes': 'x', 'images': [png, pdf]})
        draft = d.submissions.get()
        self.assertEqual(draft.images.count(), 2)         # both ride the same reply
        self.assertEqual(sum(1 for a in draft.images.all() if a.is_pdf), 1)

    def test_save_rejects_pdf_with_fake_bytes(self):
        d = _dispute()
        DisputeSubmission.objects.create(dispute=d, notes='x', source='MANUAL', status='DRAFT')
        fake = SimpleUploadedFile('evil.pdf', b'NOTPDF', content_type='application/pdf')
        self.web.post(reverse('disputes:dispute_prepare_submission', args=[d.id]),
                      {'action': 'save', 'notes': 'x', 'images': fake})
        self.assertEqual(DisputeSubmissionImage.objects.count(), 0)  # extension spoofing blocked


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
        # Assert the VIEW's own flash message, not the template's standing banner:
        # the view says "accepting a submission", the banner says "accepting a reply".
        msgs = [str(m) for m in resp.context['messages']]
        self.assertTrue(any('accepting a submission' in m for m in msgs),
                        f"expected the no-endpoint flash message; got {msgs}")


class RefreshFromPayPalTests(_UITestBase):
    """The per-dispute "Refresh from PayPal" view: pulls the latest state (and the
    buyer/PayPal messages the thread reads) on demand."""

    def test_refresh_calls_sync_and_flashes_success(self):
        d = _dispute(paypal_dispute_id='PP-D-REF1')
        with patch('apps.payments.paypal_disputes_service.sync_dispute_from_paypal') as sync:
            resp = self.web.post(reverse('disputes:dispute_refresh_from_paypal', args=[d.id]), follow=True)
            sync.assert_called_once_with('PP-D-REF1')
        self.assertEqual(resp.status_code, 200)
        msgs = [str(m) for m in resp.context['messages']]
        self.assertTrue(any('Refreshed from PayPal' in m for m in msgs), msgs)

    def test_refresh_skips_manual_disputes(self):
        # Manually-created (MANUAL-*) disputes have no real PayPal case — the guard
        # must skip the API call (else it would 404 against a synthetic id).
        d = _dispute(paypal_dispute_id='MANUAL-5-1700000000')
        with patch('apps.payments.paypal_disputes_service.sync_dispute_from_paypal') as sync:
            resp = self.web.post(reverse('disputes:dispute_refresh_from_paypal', args=[d.id]), follow=True)
            sync.assert_not_called()
        msgs = [str(m) for m in resp.context['messages']]
        self.assertTrue(any('nothing to refresh' in m.lower() for m in msgs), msgs)

    def test_refresh_handles_service_error_without_500(self):
        d = _dispute(paypal_dispute_id='PP-D-REF2')
        with patch('apps.payments.paypal_disputes_service.sync_dispute_from_paypal',
                   side_effect=RuntimeError('PayPal unreachable')):
            resp = self.web.post(reverse('disputes:dispute_refresh_from_paypal', args=[d.id]), follow=True)
        self.assertEqual(resp.status_code, 200)  # error is caught, not a 500
        msgs = [str(m) for m in resp.context['messages']]
        self.assertTrue(any("couldn't refresh" in m.lower() for m in msgs), msgs)


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


class ReplyWindowReasonTests(_UITestBase):
    """When no submit endpoint is open, the composer must explain WHY in the
    manager's terms — not a vague 'inquiry or resolved' guess."""

    def test_inquiry_waiting_on_buyer_is_explained(self):
        d = _dispute(status='RECEIVED', dispute_life_cycle_stage='INQUIRY',
                     payload={'status': 'WAITING_FOR_BUYER_RESPONSE',
                              'dispute_state': 'REQUIRED_OTHER_PARTY_ACTION'})
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        self.assertEqual(resp.context['submit_endpoint'], '')        # window genuinely closed
        self.assertIn('waiting for the buyer', resp.context['reply_window_reason'].lower())
        self.assertContains(resp, 'waiting for the buyer')

    def test_resolved_is_explained(self):
        d = _dispute(status='RESOLVED_WON', payload={'status': 'RESOLVED'})
        resp = self.web.get(reverse('disputes:dispute_detail', args=[d.id]))
        self.assertIn('resolved', resp.context['reply_window_reason'].lower())
