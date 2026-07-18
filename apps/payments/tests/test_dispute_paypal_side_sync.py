"""Red-phase spec — make colleague-made, PayPal-website-side dispute activity
visible in LORA.

A colleague can respond to a PayPal dispute DIRECTLY on paypal.com (outside
LORA). Two features close that blind spot, and NONE of the NEW behavior exists
yet — every test below must FAIL on the missing feature (not on a fixture/login
error) until it lands:

A. fetch_dispute_details(dispute_id, timeout=None) — a new optional `timeout`
   forwarded to urllib.request.urlopen(..., timeout=...). RED: the current
   one-arg signature raises TypeError on the `timeout` kwarg.

B. sync_dispute_from_paypal(dispute_id, *, timeout=None) — forwards `timeout`
   to fetch_dispute_details. RED: TypeError on the `timeout` kwarg.

C. refresh_dispute_for_view(dispute) — best-effort on-load re-pull. Skips
   synthetic (MANUAL-*) and terminal disputes, otherwise calls
   sync_dispute_from_paypal once (with a bounded int timeout), and NEVER raises.
   RED: AttributeError — pds.refresh_dispute_for_view does not exist.

D. dispute_detail view auto-refreshes on open by calling
   frontend_views.refresh_dispute_for_view, defensively (a refresh failure must
   not break the page). RED: AttributeError — the name isn't imported into
   frontend_views.

E. build_dispute_reply_timeline attribution + dedup for SUBMITTED_BY_SELLER
   evidences[] and SELLER messages[]:
     - a PayPal-side seller reply with NO matching LORA submission →
       actor 'Airport Lost Found', via_paypal True, title containing 'PayPal';
     - a PayPal record that merely MIRRORS a reply we already sent through LORA
       (normalized-equal notes) is deduped — the LORA submission entry stands
       alone. RED: no via_paypal flag and no dedup in the current builder.

F. The detail page shows an 'on PayPal' marker for a PayPal-direct seller reply,
   and none for an all-LORA (mirror-deduped) dispute. RED: the badge/label
   doesn't exist yet.

Everything that would touch PayPal is mocked: the transport (urlopen), the
sync/fetch collaborators, and the on-load refresh. NEW service functions are
reached as module attributes (pds.refresh_dispute_for_view) so a missing
function is a clean AttributeError; the view is reached via reverse().
"""

import itertools
import json
from datetime import datetime, timezone as dt_tz
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.payments import paypal_disputes_service as pds
from apps.payments.document_service import build_dispute_reply_timeline
from apps.payments.models import Dispute, DisputeSubmission

User = get_user_model()

# Globally-unique default paypal_dispute_id per created dispute so the
# get-by-id service lookups never collide (each test still passes an explicit id
# where the id is load-bearing).
_ids = itertools.count(1)


def _dispute(payload=None, **kw):
    """A normal, non-terminal, non-manual dispute. Pass `payload` for the stored
    raw PayPal record and any field overrides via kwargs."""
    base = dict(
        paypal_dispute_id=f'PP-D-SIDE{next(_ids)}',
        buyer_email='b@example.com',
        transaction_id='TX',
        transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
        dispute_reason='UNAUTHORISED',
        status='MATCHED',
        raw_webhook_payload=payload if payload is not None else {},
    )
    base.update(kw)
    return Dispute.objects.create(**base)


def _urlopen_cm(payload: dict):
    """A context-manager mock matching `with urllib.request.urlopen(...) as r:
    r.read()` — `.read()` yields the JSON body as bytes."""
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps(payload).encode('utf-8')
    cm.__exit__.return_value = False
    return cm


def _entries_with(timeline, marker):
    """Timeline entries whose text carries a unique marker (unambiguous match)."""
    return [e for e in timeline if marker in (e.get('text') or '')]


# ---------------------------------------------------------------------------
# A. fetch_dispute_details(dispute_id, timeout=None)
# ---------------------------------------------------------------------------
class FetchDisputeDetailsTimeoutTests(TestCase):
    """The new optional `timeout` reaches urlopen; default behavior is intact.

    RED: fetch_dispute_details currently takes only (dispute_id) → calling it
    with timeout=8 raises TypeError.
    """

    def test_timeout_is_forwarded_to_urlopen(self):
        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen') as mock_open:
            mock_open.return_value = _urlopen_cm(
                {'dispute_id': 'PP-D-1', 'status': 'UNDER_REVIEW'})
            result = pds.fetch_dispute_details('PP-D-1', timeout=8)

        # The function swallows HTTPError/URLError and never truly retries, so a
        # success is exactly one urlopen call.
        self.assertEqual(mock_open.call_count, 1,
                         "exactly one urlopen call (the @retry never fires on success)")
        call = mock_open.call_args
        passed = call.kwargs.get('timeout')
        if passed is None and len(call.args) >= 3:   # tolerate a positional timeout
            passed = call.args[2]
        self.assertEqual(passed, 8, "the timeout kwarg must reach urlopen")
        self.assertEqual(result, {'dispute_id': 'PP-D-1', 'status': 'UNDER_REVIEW'})

    def test_no_timeout_still_returns_the_parsed_dict(self):
        # Guard: the default (no timeout) path keeps working and returns the dict.
        with patch.object(pds, 'get_paypal_access_token', return_value='tok'), \
             patch('urllib.request.urlopen') as mock_open:
            mock_open.return_value = _urlopen_cm(
                {'dispute_id': 'PP-D-2', 'status': 'UNDER_REVIEW'})
            result = pds.fetch_dispute_details('PP-D-2')
        self.assertEqual(result, {'dispute_id': 'PP-D-2', 'status': 'UNDER_REVIEW'})


# ---------------------------------------------------------------------------
# B. sync_dispute_from_paypal(dispute_id, *, timeout=None)
# ---------------------------------------------------------------------------
class SyncDisputeFromPaypalTimeoutTests(TestCase):
    """sync forwards its `timeout` to fetch_dispute_details; default still syncs.

    RED: sync_dispute_from_paypal currently takes only (dispute_id) → the
    timeout kwarg raises TypeError.
    """

    def test_timeout_is_forwarded_to_fetch(self):
        _dispute(paypal_dispute_id='PP-D-SYNC')
        with patch.object(pds, 'fetch_dispute_details',
                          return_value={'dispute_life_cycle_stage': 'CHARGEBACK',
                                        'status': 'UNDER_REVIEW'}) as fetch:
            pds.sync_dispute_from_paypal('PP-D-SYNC', timeout=8)
        fetch.assert_called_once()
        call = fetch.call_args
        passed = call.kwargs.get('timeout')
        if passed is None and len(call.args) >= 2:   # tolerate a positional timeout
            passed = call.args[1]
        self.assertEqual(passed, 8,
                         "sync must forward its timeout to fetch_dispute_details")

    def test_no_timeout_still_syncs_the_row(self):
        # Guard: the default (no timeout) path fetches and updates the row.
        d = _dispute(paypal_dispute_id='PP-D-SYNC2', dispute_life_cycle_stage='')
        with patch.object(pds, 'fetch_dispute_details',
                          return_value={'dispute_life_cycle_stage': 'CHARGEBACK',
                                        'status': 'UNDER_REVIEW'}) as fetch:
            pds.sync_dispute_from_paypal('PP-D-SYNC2')
        fetch.assert_called_once()
        d.refresh_from_db()
        self.assertEqual(d.dispute_life_cycle_stage, 'CHARGEBACK')


# ---------------------------------------------------------------------------
# C. refresh_dispute_for_view(dispute) — best-effort on-load refresh
# ---------------------------------------------------------------------------
class RefreshDisputeForViewTests(TestCase):
    """The bounded, best-effort re-pull used when a dispute page is opened.

    RED: pds.refresh_dispute_for_view does not exist → AttributeError.
    """

    def test_manual_dispute_is_not_synced(self):
        d = _dispute(paypal_dispute_id='MANUAL-9-1700000000', status='MATCHED')
        with patch.object(pds, 'sync_dispute_from_paypal') as sync:
            pds.refresh_dispute_for_view(d)
            sync.assert_not_called()

    def test_terminal_dispute_is_not_synced(self):
        d = _dispute(paypal_dispute_id='PP-D-TERM',
                     status=Dispute.STATUS_RESOLVED_WON)
        with patch.object(pds, 'sync_dispute_from_paypal') as sync:
            pds.refresh_dispute_for_view(d)
            sync.assert_not_called()

    def test_open_dispute_is_synced_once_with_a_bounded_timeout(self):
        d = _dispute(paypal_dispute_id='PP-D-OPEN', status='MATCHED')
        with patch.object(pds, 'sync_dispute_from_paypal') as sync:
            pds.refresh_dispute_for_view(d)
        sync.assert_called_once()
        call = sync.call_args
        passed_id = call.args[0] if call.args else call.kwargs.get('dispute_id')
        self.assertEqual(passed_id, 'PP-D-OPEN',
                         "must sync THIS dispute's PayPal id")
        timeout = call.kwargs.get('timeout')
        self.assertIsInstance(timeout, int,
                              "refresh must pass a bounded int timeout to sync")
        self.assertGreater(timeout, 0)

    def test_never_raises_when_sync_fails(self):
        d = _dispute(paypal_dispute_id='PP-D-OPEN2', status='MATCHED')
        with patch.object(pds, 'sync_dispute_from_paypal',
                          side_effect=RuntimeError('paypal down')):
            # Must swallow the failure and return None — the page load can't break.
            result = pds.refresh_dispute_for_view(d)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# D. Auto-refresh wired into dispute_detail (GET /manager/disputes/<id>/)
# ---------------------------------------------------------------------------
class DetailViewAutoRefreshTests(TestCase):
    """Opening the detail page re-pulls the dispute first, defensively.

    RED: refresh_dispute_for_view is not imported into frontend_views, so the
    patch target is missing → AttributeError (no create=True, on purpose, so the
    missing import is the failure).
    """

    def setUp(self):
        self.user = User.objects.create_user(username='refresh_view_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.user)

    def _url(self, dispute):
        return reverse('disputes:dispute_detail', args=[dispute.id])

    def test_open_dispute_get_triggers_refresh_once(self):
        d = _dispute(paypal_dispute_id='PP-D-VIEW-OK', status='MATCHED',
                     payload={'dispute_state': 'REQUIRED_ACTION'})
        with patch('apps.payments.frontend_views.refresh_dispute_for_view') as mock_refresh:
            resp = self.web.get(self._url(d))
        self.assertEqual(resp.status_code, 200)
        mock_refresh.assert_called_once()
        call = mock_refresh.call_args
        passed = call.args[0] if call.args else call.kwargs.get('dispute')
        self.assertEqual(getattr(passed, 'pk', None), d.pk,
                         "the view must refresh the dispute it is displaying")

    def test_refresh_failure_does_not_break_the_page(self):
        d = _dispute(paypal_dispute_id='PP-D-VIEW-FAIL', status='MATCHED',
                     payload={'dispute_state': 'REQUIRED_ACTION'})
        with patch('apps.payments.frontend_views.refresh_dispute_for_view',
                   side_effect=RuntimeError('boom')):
            resp = self.web.get(self._url(d))
        self.assertEqual(resp.status_code, 200,
                         "a refresh failure must not 500 the detail page")


# ---------------------------------------------------------------------------
# E. build_dispute_reply_timeline — attribution + dedup
# ---------------------------------------------------------------------------
class TimelineAttributionAndDedupTests(TestCase):
    """PayPal-side seller replies are attributed 'on PayPal directly'; mirrors of
    our own LORA replies are deduped.

    RED: the current builder has no via_paypal flag and no dedup of
    SUBMITTED_BY_SELLER evidences / SELLER messages against our submissions.
    """

    def test_seller_evidence_mirroring_a_submission_is_deduped(self):
        marker = 'DEDUPEV-4821'
        text = f'{marker} our recovery service was performed and updates were sent'
        d = _dispute(paypal_dispute_id='PP-D-E1', payload={'evidences': [
            {'evidence_type': 'PROOF_OF_FULFILLMENT', 'source': 'SUBMITTED_BY_SELLER',
             # Same words, PayPal-side whitespace noise — normalizes equal.
             'notes': f'   {marker}   our recovery service was   performed and updates were sent  ',
             'date': '2026-07-10T10:00:00.000Z'}]})
        DisputeSubmission.objects.create(
            dispute=d, notes=text, kind=DisputeSubmission.KIND_EVIDENCE,
            status=DisputeSubmission.STATUS_SUBMITTED, source=DisputeSubmission.SOURCE_MANUAL)

        tl = build_dispute_reply_timeline(d)
        matches = _entries_with(tl, marker)
        self.assertEqual(
            len(matches), 1,
            f"the PayPal mirror must be deduped; got {[(e.get('kind'), e.get('title')) for e in matches]}")
        self.assertEqual(matches[0]['kind'], 'submission',
                         "the surviving entry must be our LORA submission")
        self.assertFalse(matches[0].get('via_paypal'),
                         "a LORA submission entry is not a PayPal-direct entry")

    def test_paypal_direct_seller_evidence_is_flagged_via_paypal(self):
        marker = 'DIRECTEV-5930'
        d = _dispute(paypal_dispute_id='PP-D-E2', payload={'evidences': [
            {'evidence_type': 'PROOF_OF_FULFILLMENT', 'source': 'SUBMITTED_BY_SELLER',
             'notes': f'{marker} a colleague answered the buyer through the resolution center',
             'date': '2026-07-10T10:00:00.000Z'}]})

        tl = build_dispute_reply_timeline(d)
        matches = _entries_with(tl, marker)
        self.assertEqual(len(matches), 1, "the PayPal-direct evidence must appear once")
        entry = matches[0]
        self.assertEqual(entry['actor'], 'Airport Lost Found')
        self.assertTrue(entry.get('via_paypal'),
                        "a seller reply made on PayPal's site must set via_paypal")
        self.assertIn('PayPal', entry['title'],
                      "the title must call out it was submitted on PayPal")
        self.assertIn(marker, entry['text'])

    def test_seller_message_mirroring_a_submission_is_deduped(self):
        marker = 'DEDUPMSG-6014'
        text = f'{marker} we have located your item and will be in touch shortly'
        d = _dispute(paypal_dispute_id='PP-D-E3', payload={'messages': [
            {'posted_by': 'SELLER',
             'content': f'  {marker}  we have located your item and will be in touch   shortly ',
             'time_posted': '2026-07-11T10:00:00.000Z'}]})
        DisputeSubmission.objects.create(
            dispute=d, notes=text, kind=DisputeSubmission.KIND_MESSAGE,
            status=DisputeSubmission.STATUS_SUBMITTED, source=DisputeSubmission.SOURCE_MANUAL)

        tl = build_dispute_reply_timeline(d)
        matches = _entries_with(tl, marker)
        self.assertEqual(
            len(matches), 1,
            f"the mirrored SELLER message must be deduped; got {[(e.get('kind'), e.get('title')) for e in matches]}")
        self.assertEqual(matches[0]['kind'], 'submission',
                         "the surviving entry must be our LORA message submission")
        self.assertFalse(matches[0].get('via_paypal'))

    def test_paypal_direct_seller_message_is_flagged_via_paypal(self):
        marker = 'DIRECTMSG-7725'
        d = _dispute(paypal_dispute_id='PP-D-E4', payload={'messages': [
            {'posted_by': 'SELLER',
             'content': f'{marker} a colleague messaged the buyer straight from the resolution center',
             'time_posted': '2026-07-11T10:00:00.000Z'}]})

        tl = build_dispute_reply_timeline(d)
        matches = _entries_with(tl, marker)
        self.assertEqual(len(matches), 1, "the PayPal-direct message must appear once")
        entry = matches[0]
        self.assertEqual(entry['actor'], 'Airport Lost Found')
        self.assertTrue(entry.get('via_paypal'),
                        "a message sent on PayPal's site must set via_paypal")
        self.assertIn('PayPal', entry['title'],
                      "the title must call out it was sent on PayPal")

    def test_buyer_message_is_unaffected(self):
        # Guard: buyer messages still read as the Buyer and are never via_paypal.
        marker = 'BUYERMSG-8836'
        d = _dispute(paypal_dispute_id='PP-D-E5A', payload={'messages': [
            {'posted_by': 'BUYER', 'content': f'{marker} I still want my money back',
             'time_posted': '2026-07-11T10:00:00.000Z'}]})
        tl = build_dispute_reply_timeline(d)
        matches = _entries_with(tl, marker)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['actor'], 'Buyer')
        self.assertFalse(matches[0].get('via_paypal'))

    def test_regular_lora_submission_is_not_flagged_via_paypal(self):
        # Guard: a normal LORA-native submission entry has a falsy via_paypal.
        marker = 'LORASUB-9947'
        d = _dispute(paypal_dispute_id='PP-D-E5B')
        DisputeSubmission.objects.create(
            dispute=d, notes=f'{marker} our formal evidence narrative',
            kind=DisputeSubmission.KIND_EVIDENCE,
            status=DisputeSubmission.STATUS_SUBMITTED, source=DisputeSubmission.SOURCE_MANUAL)
        tl = build_dispute_reply_timeline(d)
        matches = _entries_with(tl, marker)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['kind'], 'submission')
        self.assertFalse(matches[0].get('via_paypal'))


# ---------------------------------------------------------------------------
# F. Detail page renders an 'on PayPal' marker for PayPal-direct replies
# ---------------------------------------------------------------------------
class DetailPagePaypalDirectBadgeTests(TestCase):
    """The rendered detail page shows a visible 'on PayPal' marker when a seller
    reply originated on PayPal's site, and none when everything is LORA-native.

    RED: no such badge/label exists → the positive assertion fails ('on PayPal'
    absent). The negative case is a guard (absent in both phases).

    sync_dispute_from_paypal is patched so that once the on-load refresh (D/C)
    lands, the page render stays hermetic (no network, stored payload intact).
    """

    def setUp(self):
        self.user = User.objects.create_user(username='badge_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.user)

    def _html(self, dispute):
        with patch.object(pds, 'sync_dispute_from_paypal'):
            resp = self.web.get(reverse('disputes:dispute_detail', args=[dispute.id]))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_paypal_direct_evidence_renders_on_paypal_marker(self):
        marker = 'PPDIRECTBADGE-3310'   # deliberately does NOT contain 'on PayPal'
        d = _dispute(paypal_dispute_id='PP-D-FPOS', status='MATCHED',
                     payload={'dispute_state': 'REQUIRED_ACTION', 'evidences': [
                         {'evidence_type': 'PROOF_OF_FULFILLMENT',
                          'source': 'SUBMITTED_BY_SELLER',
                          'notes': f'{marker} a colleague replied through the resolution center',
                          'date': '2026-07-10T10:00:00.000Z'}]})
        html = self._html(d)
        self.assertIn('on PayPal', html,
                      "a PayPal-origin seller reply must render an 'on PayPal' marker")

    def test_lora_only_dispute_has_no_on_paypal_marker(self):
        # Guard: an all-LORA dispute (its PayPal mirror deduped) shows no marker.
        marker = 'LORAONLY-2201'
        text = f'{marker} our recovery service record'
        d = _dispute(paypal_dispute_id='PP-D-FNEG', status='MATCHED',
                     payload={'dispute_state': 'REQUIRED_ACTION', 'evidences': [
                         {'evidence_type': 'PROOF_OF_FULFILLMENT',
                          'source': 'SUBMITTED_BY_SELLER',
                          'notes': text, 'date': '2026-07-10T10:00:00.000Z'}]})
        DisputeSubmission.objects.create(
            dispute=d, notes=text, kind=DisputeSubmission.KIND_EVIDENCE,
            status=DisputeSubmission.STATUS_SUBMITTED, source=DisputeSubmission.SOURCE_MANUAL)
        html = self._html(d)
        self.assertNotIn('on PayPal', html,
                         "an all-LORA dispute (mirror deduped) must not show the marker")
