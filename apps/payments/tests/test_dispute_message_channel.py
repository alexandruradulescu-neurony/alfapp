"""Red-phase spec — the PayPal "message to buyer" reply channel.

At PayPal's INQUIRY stage a seller cannot upload formal evidence (the
provide-evidence window is closed) but CAN still message the buyer through the
Resolution Center. LORA already carries the raw transport but has no in-app
action or orchestration for it. This file pins the NEW "Message the buyer"
feature (deliberately unimplemented — these tests must FAIL until it lands):

1. Dispute.can_message — a boolean gate for whether PayPal's message channel is
   open. MANUAL-* synthetic ids, terminal statuses and RESOLVED payloads close
   it; INQUIRY / under-review / waiting-for-buyer keep it open. NB the contrast
   with submit_endpoint: at INQUIRY there is NO evidence window
   (submit_endpoint == '') yet the message channel IS open.
2. pds._post_dispute_message(dispute_id, message) — a pure JSON transport that
   POSTs {"message": ...} to .../{id}/send-message via paypal_json_request and
   returns (ok: bool, response: dict).
3. pds.send_dispute_message(dispute, message, *, performed_by=None) — records a
   KIND_MESSAGE DisputeSubmission and sends it through the transport, re-syncing
   from PayPal on success.
4. dispute_send_message view + the disputes:dispute_send_message URL
   (POST /manager/disputes/<id>/send-message/), login-only.
5. build_dispute_reply_timeline renders a sent MESSAGE as "Message sent"
   (already built — this guards the display contract and MAY already pass).
6. The detail page shows the send-message form only while can_message is True.

Nothing here talks to PayPal: the transport (_post_dispute_message /
paypal_json_request) and the post-send re-sync (sync_dispute_from_paypal) are
mocked. New service functions are reached as module attributes
(pds.send_dispute_message / pds._post_dispute_message) so a missing function is
a clean AttributeError, and the view/URL are reached via reverse() so a missing
route is a clean NoReverseMatch.
"""

import itertools
import urllib.error
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import constants as message_constants
from django.test import Client, TestCase
from django.urls import reverse

from apps.payments import paypal_disputes_service as pds
from apps.payments.document_service import build_dispute_reply_timeline
from apps.payments.models import (Dispute, DisputeActivityLog, DisputeSubmission)

User = get_user_model()

# Globally-unique paypal_dispute_id per created dispute so the get-by-id lookups
# the service does never collide.
_ids = itertools.count(1)


def _dispute(payload=None, **kw):
    """A dispute with a guaranteed-unique paypal_dispute_id (unless one is passed
    explicitly). Defaults are a normal, non-terminal, non-manual case."""
    base = dict(
        paypal_dispute_id=f'PP-D-MSG{next(_ids)}',
        buyer_email='b@example.com',
        transaction_id='TX',
        transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
        dispute_reason='UNAUTHORISED',
        status='MATCHED',
        raw_webhook_payload=payload if payload is not None else {},
    )
    base.update(kw)
    return Dispute.objects.create(**base)


def _messageable_dispute(**kw):
    """INQUIRY-stage dispute: PayPal's evidence window is CLOSED
    (submit_endpoint == '') but the message channel is OPEN (can_message True)."""
    return _dispute(payload={'dispute_state': 'REQUIRED_ACTION'},
                    dispute_life_cycle_stage='INQUIRY', **kw)


def _resolved_dispute(**kw):
    """A closed dispute — can_message must be False."""
    return _dispute(status=Dispute.STATUS_RESOLVED_WON,
                    payload={'dispute_state': 'RESOLVED'}, **kw)


class CanMessagePropertyTests(TestCase):
    """Truth table for Dispute.can_message (property does NOT exist yet →
    AttributeError until it lands)."""

    def test_manual_id_closes_the_channel(self):
        # A synthetic MANUAL-* dispute has no real PayPal case to message — even
        # though it would otherwise (INQUIRY) be open.
        d = _dispute(paypal_dispute_id='MANUAL-9-1700000000',
                     payload={'dispute_state': 'REQUIRED_ACTION'},
                     dispute_life_cycle_stage='INQUIRY')
        self.assertFalse(d.can_message)

    def test_terminal_resolved_won_closes_the_channel(self):
        d = _dispute(status=Dispute.STATUS_RESOLVED_WON)
        self.assertFalse(d.can_message)

    def test_terminal_resolved_lost_closes_the_channel(self):
        d = _dispute(status=Dispute.STATUS_RESOLVED_LOST)
        self.assertFalse(d.can_message)

    def test_terminal_accepted_closes_the_channel(self):
        d = _dispute(status=Dispute.STATUS_ACCEPTED)
        self.assertFalse(d.can_message)

    def test_payload_dispute_state_resolved_closes_the_channel(self):
        d = _dispute(status='MATCHED', payload={'dispute_state': 'RESOLVED'})
        self.assertFalse(d.can_message)

    def test_payload_status_resolved_closes_the_channel(self):
        d = _dispute(status='MATCHED', payload={'status': 'RESOLVED'})
        self.assertFalse(d.can_message)

    def test_inquiry_is_messageable(self):
        d = _dispute(payload={'dispute_state': 'REQUIRED_ACTION'},
                     dispute_life_cycle_stage='INQUIRY')
        self.assertTrue(d.can_message)

    def test_under_paypal_review_is_messageable(self):
        d = _dispute(payload={'dispute_state': 'UNDER_PAYPAL_REVIEW'})
        self.assertTrue(d.can_message)

    def test_waiting_for_buyer_response_is_messageable(self):
        d = _dispute(payload={'status': 'WAITING_FOR_BUYER_RESPONSE'})
        self.assertTrue(d.can_message)

    def test_inquiry_has_no_evidence_window_but_is_messageable(self):
        # The load-bearing contrast: at INQUIRY there is NO evidence endpoint,
        # yet the buyer-message channel is open.
        d = _messageable_dispute()
        self.assertEqual(d.submit_endpoint, '')
        self.assertIs(d.can_message, True)


class PostDisputeMessageTransportTests(TestCase):
    """pds._post_dispute_message: pure JSON transport, returns (ok, dict)."""

    def test_success_posts_message_and_returns_response(self):
        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch.object(pds, 'paypal_json_request', return_value={'links': []}) as pj:
            ok, resp = pds._post_dispute_message('PP-D-MSG-TX', 'hi buyer')
        self.assertTrue(ok)
        self.assertEqual(resp, {'links': []})
        pj.assert_called_once()
        call = pj.call_args
        url = call.args[0] if call.args else call.kwargs.get('url')
        self.assertTrue(url.endswith('/send-message'),
                        f"must POST to .../send-message; got {url!r}")
        self.assertIn('PP-D-MSG-TX', url)
        self.assertEqual(call.kwargs.get('method'), 'POST')
        self.assertEqual(call.kwargs.get('payload'), {'message': 'hi buyer'})

    def test_transport_error_returns_false_and_a_dict(self):
        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch.object(pds, 'paypal_json_request',
                          side_effect=urllib.error.URLError('boom')):
            ok, resp = pds._post_dispute_message('PP-D-MSG-ERR', 'hi buyer')
        self.assertFalse(ok)
        self.assertIsInstance(resp, dict)

    def test_no_token_fails_without_calling_network(self):
        with patch.object(pds, 'get_paypal_access_token', return_value=None), \
             patch.object(pds, 'paypal_json_request') as pj:
            ok, resp = pds._post_dispute_message('PP-D-MSG-NOTOK', 'hi buyer')
            pj.assert_not_called()
        self.assertFalse(ok)
        self.assertIsInstance(resp, dict)


class SendDisputeMessageOrchestrationTests(TestCase):
    """pds.send_dispute_message records a KIND_MESSAGE submission and sends it.

    The transport (_post_dispute_message) and the post-send re-sync
    (sync_dispute_from_paypal) are patched; _post_dispute_message uses
    create=True so the collaborator patch succeeds and the red-phase failure
    lands cleanly on the missing send_dispute_message itself.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='msg_orch_mgr', password='x')

    def test_happy_path_records_submitted_message_and_resyncs(self):
        d = _messageable_dispute(paypal_dispute_id='PP-D-MSG-OK')
        with patch.object(pds, '_post_dispute_message',
                          return_value=(True, {'ok': True}), create=True), \
             patch.object(pds, 'sync_dispute_from_paypal') as sync:
            result = pds.send_dispute_message(d, 'hello buyer')
            sync.assert_called_once_with(d.paypal_dispute_id)
        self.assertTrue(result)

        self.assertEqual(d.submissions.count(), 1,
                         "exactly one message submission must be recorded")
        sub = d.submissions.get()
        self.assertEqual(sub.kind, DisputeSubmission.KIND_MESSAGE)
        self.assertEqual(sub.status, DisputeSubmission.STATUS_SUBMITTED)
        self.assertEqual(sub.notes, 'hello buyer')
        self.assertIsNotNone(sub.submitted_at)
        self.assertFalse(sub.attach_evidence_pdf,
                         "a buyer message carries no evidence PDF")
        self.assertFalse(sub.attach_terms,
                         "a buyer message must not attach the T&C (model default is True)")
        self.assertFalse(sub.attach_invoice,
                         "a buyer message must not attach the invoice (model default is True)")

        logs = list(DisputeActivityLog.objects.filter(dispute=d))
        good = [l for l in logs
                if 'message' in l.details.lower()
                and 'no attachments' not in l.details.lower()]
        self.assertTrue(
            good,
            "need an activity-log row mentioning 'message' and NOT reusing the "
            f"evidence 'No attachments' wording; got {[l.details for l in logs]}")

    def test_failure_marks_submission_failed_and_skips_resync(self):
        d = _messageable_dispute(paypal_dispute_id='PP-D-MSG-FAIL')
        with patch.object(pds, '_post_dispute_message',
                          return_value=(False, {'error': 'http_error', 'code': 422}),
                          create=True), \
             patch.object(pds, 'sync_dispute_from_paypal') as sync:
            result = pds.send_dispute_message(d, 'hello buyer')
            sync.assert_not_called()
        self.assertFalse(result)
        sub = d.submissions.get()
        self.assertEqual(sub.status, DisputeSubmission.STATUS_FAILED)
        self.assertEqual(sub.kind, DisputeSubmission.KIND_MESSAGE)
        self.assertEqual(sub.notes, 'hello buyer')

    def test_performed_by_is_recorded_as_submitted_by(self):
        d = _messageable_dispute(paypal_dispute_id='PP-D-MSG-USER')
        with patch.object(pds, '_post_dispute_message',
                          return_value=(True, {'ok': True}), create=True), \
             patch.object(pds, 'sync_dispute_from_paypal'):
            result = pds.send_dispute_message(d, 'hello buyer', performed_by=self.user)
        self.assertTrue(result)
        sub = d.submissions.get()
        self.assertEqual(sub.submitted_by, self.user)


class DisputeSendMessageViewTests(TestCase):
    """The dispute_send_message POST view + disputes:dispute_send_message URL.

    reverse() is called BEFORE the send_dispute_message patch so the missing
    route is a clean NoReverseMatch in the red phase.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='msg_view_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.user)

    def _post(self, dispute, data, *, send_return=True):
        url = reverse('disputes:dispute_send_message', args=[dispute.id])
        with patch('apps.payments.frontend_views.send_dispute_message',
                   return_value=send_return) as mock_send:
            resp = self.web.post(url, data, follow=True)
        return resp, mock_send

    def _levels(self, resp):
        return [m.level for m in resp.context['messages']]

    def _assert_redirects_to_detail(self, resp, dispute):
        self.assertTrue(resp.redirect_chain, "the view must redirect")
        self.assertTrue(
            resp.redirect_chain[-1][0].endswith(f'/manager/disputes/{dispute.id}/'),
            f"must land on the dispute detail page; chain={resp.redirect_chain}")

    def test_blank_message_is_rejected_without_calling_the_service(self):
        d = _messageable_dispute(paypal_dispute_id='PP-D-MSG-VBLANK')
        resp, mock_send = self._post(d, {'message': '   '})
        mock_send.assert_not_called()
        self._assert_redirects_to_detail(resp, d)
        self.assertIn(message_constants.ERROR, self._levels(resp),
                      "a blank message must queue an error flash")

    def test_unmessageable_dispute_is_rejected_without_calling_the_service(self):
        d = _resolved_dispute(paypal_dispute_id='PP-D-MSG-VRESOLVED')
        resp, mock_send = self._post(d, {'message': 'are you there?'})
        mock_send.assert_not_called()
        self._assert_redirects_to_detail(resp, d)
        self.assertIn(message_constants.ERROR, self._levels(resp),
                      "a closed dispute must queue an error flash")

    def test_happy_path_calls_service_and_flashes_success(self):
        d = _messageable_dispute(paypal_dispute_id='PP-D-MSG-VOK')
        message = 'Please check your email about the recovered item.'
        resp, mock_send = self._post(d, {'message': message}, send_return=True)
        mock_send.assert_called_once()
        call = mock_send.call_args
        passed_dispute = call.args[0] if call.args else call.kwargs.get('dispute')
        passed_message = (call.args[1] if len(call.args) > 1
                          else call.kwargs.get('message'))
        self.assertEqual(getattr(passed_dispute, 'pk', None), d.pk,
                         "the view must pass the dispute to send_dispute_message")
        self.assertEqual(passed_message, message,
                         "the view must pass the posted message text through")
        self._assert_redirects_to_detail(resp, d)
        self.assertIn(message_constants.SUCCESS, self._levels(resp),
                      "a successful send must queue a success flash")

    def test_service_failure_flashes_error_and_still_redirects(self):
        d = _messageable_dispute(paypal_dispute_id='PP-D-MSG-VFAIL')
        resp, mock_send = self._post(
            d, {'message': 'Please check your email.'}, send_return=False)
        mock_send.assert_called_once()
        self._assert_redirects_to_detail(resp, d)
        self.assertIn(message_constants.ERROR, self._levels(resp),
                      "a rejected send must queue an error flash")

    def test_get_is_not_allowed(self):
        d = _messageable_dispute(paypal_dispute_id='PP-D-MSG-VGET')
        url = reverse('disputes:dispute_send_message', args=[d.id])
        resp = self.web.get(url)
        self.assertEqual(resp.status_code, 405)   # @require_POST


class MessageTimelineTests(TestCase):
    """A sent MESSAGE submission renders as a 'Message sent' timeline entry.

    build_dispute_reply_timeline already handles KIND_MESSAGE, so this guards
    the display contract and MAY already pass.
    """

    def test_sent_message_renders_as_message_sent(self):
        d = _messageable_dispute(paypal_dispute_id='PP-D-MSG-TL')
        DisputeSubmission.objects.create(
            dispute=d, kind=DisputeSubmission.KIND_MESSAGE,
            status=DisputeSubmission.STATUS_SUBMITTED, notes='hello buyer')
        timeline = build_dispute_reply_timeline(d)
        sent = [e for e in timeline if e.get('title') == 'Message sent']
        self.assertEqual(len(sent), 1,
                         f"expected one 'Message sent' entry; got {timeline}")
        entry = sent[0]
        self.assertEqual(entry['actor'], 'Airport Lost Found')
        self.assertIn('hello buyer', entry['text'])


class DetailPageMessageFormTests(TestCase):
    """The detail page exposes the send-message form only while can_message is
    True."""

    def setUp(self):
        self.user = User.objects.create_user(username='msg_detail_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.user)

    def _html(self, dispute):
        resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_messageable_dispute_shows_the_send_message_form(self):
        d = _messageable_dispute(paypal_dispute_id='PP-D-MSG-DON')
        html = self._html(d)
        self.assertIn(f'/manager/disputes/{d.id}/send-message/', html,
                      "the message form action (the send-message URL) must render")
        self.assertIn('name="message"', html,
                      "the message textarea must render")

    def test_resolved_dispute_hides_the_send_message_form(self):
        d = _resolved_dispute(paypal_dispute_id='PP-D-MSG-DOFF')
        html = self._html(d)
        self.assertNotIn(f'/manager/disputes/{d.id}/send-message/', html,
                         "a resolved dispute must not offer the message form")
