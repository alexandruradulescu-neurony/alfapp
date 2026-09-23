"""RED-phase spec for two more live-preview findings on the Case-timeline AI
writer (apps/payments/document_service.py), on top of the round-1/round-2
work pinned in test_report_timeline_rows.py / test_report_timeline_ai.py /
test_report_timeline_round2.py. Deliberately unimplemented — these tests must
fail until the behaviour lands, except where noted in the run report (a
couple of sub-cases pin behaviour the current code already gets right, as a
control/regression guard around the new checks).

Live-preview findings pinned here:

  P9  Call direction. Previewing a real dispute showed the AI writing "We
      called the customer (4m 51s)..." for a call the CUSTOMER placed TO us.
      The root cause: `_build_timeline`'s `_call_context` (the text handed to
      the 'dispute_timeline' AI call for a call row) never states which way
      the call went — only its length and any linked call-recording-summary
      text — so the AI has nothing to ground the direction in and can guess
      wrong. And `_ai_row_ok`'s per-row check for a 'call' row only verifies
      the forbidden-connection-words list and duration consistency
      (_call_duration_consistent) — nothing cross-checks the AI's stated
      direction ("We called the customer" / "The customer called us")
      against the call record's own `direction` field. This file pins: (a)
      the AI's context must say which way the call went, in the same fixed
      phrasing `_build_timeline` already uses for the deterministic fallback
      sentence; (b)/(d) an AI line that states the WRONG direction for a
      call must fall back to that plain deterministic sentence, not be kept;
      (c) a line that states the RIGHT direction is kept as written (a
      control case — already correct today, see the run report).

  P10 Internal notes are not messages to the customer. Previewing a real
      dispute also showed a purely INTERNAL case note (an agent pasting an
      airport's "We found a match!" notice into Zendesk for the record — never
      sent anywhere) narrated by the AI as "We let the customer know that
      [office] found a possible match" — implying we contacted the customer,
      when nothing was sent. The root cause: `_ai_row_ok` has no per-kind
      check at all for `_ai_kind == 'update'` (an internal update row) beyond
      the generic non-empty/length/staff-name checks — unlike 'call' and
      'filing', which each get row-kind-specific checks. This file pins: (a)
      an AI line for a substantive internal note that uses any customer-facing
      verb ("let ... know", "told", "emailed", "sent", "informed", "asked" +
      "the customer") must produce NO row at all for that note — there is no
      deterministic fallback for an 'update' row (see
      `_is_substantial_internal_update`'s docstring), so a rejected AI line
      means the row simply never appears; (b) a line that stays honestly
      internal (describing what the OFFICE reported, not what WE told the
      customer) is kept, at the note's own time (a control case — already
      correct today); (c) the same verbs are perfectly fine on a genuine EMAIL
      row (a real public reply we sent) — the new check is about the
      internal/customer-facing mismatch, not the verbs themselves (also a
      control case).

Tests build synthetic cases through build_dispute_evidence_bundle(dispute,
embed_attachments=False, use_ai=True), the resulting timeline rows, and the
captured kwargs of the patched apps.payments.document_service.AIClient.complete
call for call_site='dispute_timeline' — mirroring test_report_timeline_ai.py /
test_report_timeline_round2.py's fixture and patch style, with fresh,
self-contained fixtures (unique alf_claim_id / paypal_dispute_id per case, all
under an 'RTD' — Report Timeline Direction — prefix distinct from every other
dispute test file so parallel test runs never collide).

Run:
    ( cd /Users/alex/Code/proj-alf/alfapp/.worktrees/tl && ../../.venv/bin/python -m pytest \\
      apps/payments/tests/test_report_timeline_direction.py -o addopts="" -q -p no:cacheprovider )
"""

import types
from datetime import datetime, timezone as dt_tz
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments.models import Dispute
from apps.payments import document_service as ds

UTC = dt_tz.utc


# ---------------------------------------------------------------------------
# Shared fixture builders — mirrors test_report_timeline_round2.py's style:
# every claim/dispute gets its own auto-incrementing unique id, under a
# prefix ('RTD') no other dispute test file uses.
# ---------------------------------------------------------------------------

_SEQ = [0]


def _next_id():
    _SEQ[0] += 1
    return _SEQ[0]


def _claim(**kw):
    n = _next_id()
    base = dict(
        alf_claim_id=f'ALF-RTD-{n:04d}', client_name='Test Client',
        client_email=f'client-rtd-{n}@example.com', price_paid=Decimal('75.00'),
        zd_ticket_id=f'ZD-RTD-{n:04d}', flight_details='', flight_data={},
    )
    base.update(kw)
    return Claim.objects.create(**base)


def _dispute(claim, *, created_at=None, **kw):
    n = _next_id()
    base = dict(
        paypal_dispute_id=f'PP-RTD-{n:04d}',
        buyer_email=(claim.client_email if claim else 'buyer@example.com'),
        transaction_id=f'TX-RTD-{n:04d}',
        transaction_date=datetime(2026, 9, 18, 15, 48, 29, tzinfo=UTC),
        dispute_reason='MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED',
        dispute_amount=Decimal('75.00'), dispute_currency='USD',
        dispute_life_cycle_stage='CHARGEBACK',
        zd_ticket_id=(claim.zd_ticket_id if claim else ''),
        claim=claim, raw_webhook_payload={},
    )
    base.update(kw)
    d = Dispute.objects.create(**base)
    Dispute.objects.filter(pk=d.pk).update(
        created_at=created_at or datetime(2026, 9, 20, 14, 29, 16, tzinfo=UTC))
    d.refresh_from_db()
    return d


def _intake_comment(created_at='2026-09-18T15:12:54Z', reg_id='ALF-RTD-0000',
                    client_name='Test Client', client_email='client@example.com'):
    body = (f'Registration ID: {reg_id}\nName: {client_name}\nEmail: {client_email}\n\n'
            'Date/Time: September 18, 2026 7:00 am\nLost object: Carry-On')
    return {'author': {'name': client_name, 'email': client_email}, 'public': False,
            'channel': 'web', 'created_at': created_at, 'body': body, 'attachments': []}


def _agent_note(body, created_at, author_name='Agent One',
                author_email='agent1@alf.example', public=False):
    return {'author': {'name': author_name, 'email': author_email}, 'public': public,
            'channel': 'web', 'created_at': created_at, 'body': body, 'attachments': []}


def _voice_comment(duration, direction, created_at, answered_by='Agent One'):
    return {'author': {'name': answered_by, 'email': 'agent1@alf.example'}, 'public': False,
            'channel': 'voice', 'created_at': created_at,
            'body': f'{direction.title()} call', 'attachments': [],
            'call': {'direction': direction, 'duration': duration, 'answered_by': answered_by,
                     'recorded': True, 'started_at': created_at}}


def _call_summary_note(created_at, resolution):
    return _agent_note(
        f'**Call Recording Summary**\n\nCaller Name: Test Client\nIssue: Lost item.\n'
        f'Resolution: {resolution}\nNext Steps: Continue the investigation.',
        created_at, author_name='Agent Two', author_email='agent2@alf.example')


def _public_email(body, created_at, author_name='Agent One', author_email='agent1@alf.example'):
    return {'author': {'name': author_name, 'email': author_email}, 'public': True,
            'channel': 'email', 'created_at': created_at, 'body': body, 'attachments': []}


def _fake_rows(mapping):
    """A duck-typed TimelineActivities stand-in: {index: activity} -> an
    object with a .rows list of SimpleNamespace(index=int, activity=str)."""
    return types.SimpleNamespace(
        rows=[types.SimpleNamespace(index=i, activity=a) for i, a in mapping.items()])


def _capture(dispute, comments, fake_result=None, use_ai=True):
    """Build the bundle with the AI wired on, capturing the kwargs of the
    call to call_site='dispute_timeline' and (optionally) returning
    `fake_result` for it. Every OTHER call_site (the section-sorting
    narrator, 'dispute_evidence_narrative') raises — safe here exactly as in
    test_report_timeline_round2.py's `_capture`: `_narrate_evidence` catches
    any exception from AIClient.complete and returns None, so
    `_narrate_image_evidence` is never reached (every fixture below keeps at
    least one non-image-only item — a call or a note/email with real text —
    so text_items is always non-empty and _narrate_evidence always runs
    first)."""
    captured = {}

    def _side_effect(**kwargs):
        site = kwargs.get('call_site')
        if site == 'dispute_timeline':
            captured.update(kwargs)
            if fake_result is not None:
                if isinstance(fake_result, BaseException):
                    raise fake_result
                return fake_result
            from apps.ai.schemas import TimelineActivities
            return TimelineActivities(rows=[])
        raise Exception(f"unexpected call_site {site!r} in this test")

    with patch.object(ds, '_fetch_zendesk_ticket_full',
                      return_value={'ticket': {'id': dispute.zd_ticket_id}, 'comments': comments}), \
         patch('apps.payments.document_service.AIClient.complete', side_effect=_side_effect):
        bundle = ds.build_dispute_evidence_bundle(dispute, embed_attachments=False, use_ai=use_ai)
    return bundle, captured


class _DirectionTestCase(TestCase):
    """Shared setUp (AI key, so _narrate_timeline actually calls the AI) + a
    row-lookup helper, mirroring _TimelineAITestCase._row in
    test_report_timeline_ai.py."""

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.save()

    def _row(self, timeline, when_contains):
        matches = [e for e in timeline if when_contains in e['when']]
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one row with when containing {when_contains!r}, "
            f"got {[e['when'] for e in timeline]}")
        return matches[0]


# ---------------------------------------------------------------------------
# P9(a) — the AI's own CONTEXT for a call row must say which way it went.
# ---------------------------------------------------------------------------

class CallDirectionContextTests(_DirectionTestCase):
    """Each call gets a linked '**Call Recording Summary**' note 2 minutes
    later (within _CALL_CONTEXT_WINDOW), matching a realistic AI-eligible
    call and the shape used throughout test_report_timeline_ai.py /
    test_report_timeline_round2.py."""

    def test_inbound_call_context_says_the_customer_called_us(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _voice_comment(291, 'inbound', '2026-09-18T16:54:58Z'),
                    _call_summary_note('2026-09-18T16:56:58Z',
                                       'Confirmed the search is continuing.')]
        bundle, captured = _capture(dispute, comments)
        records = captured['untrusted']['zendesk_comment']
        self.assertEqual(len(records), 1, records)
        self.assertIn('the customer called us', records[0].lower(),
                      f"inbound call context must say the customer called us: {records[0]!r}")

    def test_outbound_call_context_says_we_called_the_customer(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _voice_comment(291, 'outbound', '2026-09-18T16:54:58Z'),
                    _call_summary_note('2026-09-18T16:56:58Z',
                                       'Confirmed the search is continuing.')]
        bundle, captured = _capture(dispute, comments)
        records = captured['untrusted']['zendesk_comment']
        self.assertEqual(len(records), 1, records)
        self.assertIn('we called the customer', records[0].lower(),
                      f"outbound call context must say we called the customer: {records[0]!r}")


# ---------------------------------------------------------------------------
# P9(b)/(c)/(d) — the per-row check must cross the AI's stated direction
# against the call's real direction; only a mismatch falls back.
# ---------------------------------------------------------------------------

class CallDirectionPerRowCheckTests(_DirectionTestCase):

    def _case(self, direction, duration):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _voice_comment(duration, direction, '2026-09-18T16:54:58Z'),
                    _call_summary_note('2026-09-18T16:56:58Z',
                                       'Confirmed the search is continuing.')]
        return dispute, comments

    def test_b_inbound_call_wrong_direction_ai_text_falls_back(self):
        # duration=291 -> '4m 51s', matching the exact case that showed up
        # in the live preview.
        dispute, comments = self._case('inbound', 291)
        bad = 'We called the customer (4m 51s) to check on the search status'
        bundle, _ = _capture(dispute, comments, fake_result=_fake_rows({0: bad}))
        row = self._row(bundle['timeline'], '11:54')
        self.assertEqual(row['text'], 'The customer called us (4m 51s)')

    def test_c_inbound_call_correct_direction_ai_text_is_kept(self):
        dispute, comments = self._case('inbound', 291)
        good = 'The customer called us (4m 51s) to ask about the search'
        bundle, _ = _capture(dispute, comments, fake_result=_fake_rows({0: good}))
        row = self._row(bundle['timeline'], '11:54')
        self.assertEqual(row['text'], good)

    def test_d_outbound_call_wrong_direction_ai_text_falls_back(self):
        dispute, comments = self._case('outbound', 47)
        bad = 'The customer called to update their contact information'
        bundle, _ = _capture(dispute, comments, fake_result=_fake_rows({0: bad}))
        row = self._row(bundle['timeline'], '11:54')
        self.assertEqual(row['text'], 'We called the customer (47 seconds)')


# ---------------------------------------------------------------------------
# P10(a)/(b) — a substantive internal note is not a message to the customer.
# ---------------------------------------------------------------------------

class InternalUpdateNoteMessagingTests(_DirectionTestCase):
    """The exact preview note: an agent logging an airport's match notice
    internally — never sent to the customer as written."""

    _NOTE_BODY = ('We found a match!\n\nHi Test Client,\nWe have great news, Orlando '
                 'International Airport (MCO) has found an item that matches your lost '
                 'item.\nPlease review the match and confirm it is yours.')

    def _case(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _agent_note(self._NOTE_BODY, '2026-09-18T19:00:00Z')]
        return dispute, comments

    def test_a_customer_facing_verbs_never_produce_a_row(self):
        variants = [
            'We let the customer know that Orlando International Airport (MCO) found a '
            'possible match',
            'We told the customer that Orlando International Airport (MCO) found a possible '
            'match',
            'We emailed the customer about the possible match found by Orlando International '
            'Airport (MCO)',
            'We sent the customer details about the possible match found by Orlando '
            'International Airport (MCO)',
            'We informed the customer that Orlando International Airport (MCO) found a '
            'possible match',
            'We asked the customer to confirm the possible match found by Orlando '
            'International Airport (MCO)',
        ]
        for activity in variants:
            with self.subTest(activity=activity):
                dispute, comments = self._case()
                bundle, _ = _capture(dispute, comments, fake_result=_fake_rows({0: activity}))
                tl = bundle['timeline']
                self.assertEqual(
                    [e for e in tl if '14:00' in e['when']], [],
                    f"an internal note described as a message TO the customer must produce "
                    f"no row at all (no deterministic fallback exists for an 'update' row): "
                    f"{tl!r}")

    def test_b_office_grounded_text_produces_a_row_with_that_text(self):
        dispute, comments = self._case()
        good = 'Orlando International Airport (MCO) reported a possible match for the lost item'
        bundle, _ = _capture(dispute, comments, fake_result=_fake_rows({0: good}))
        row = self._row(bundle['timeline'], '14:00')
        self.assertEqual(row['text'], good)


# ---------------------------------------------------------------------------
# P10(c) — the same verbs are fine on a genuine EMAIL row (a real public
# reply we sent) — the rule is about internal/customer-facing mismatch, not
# the verbs themselves.
# ---------------------------------------------------------------------------

class InternalUpdateVerbsAllowedOnEmailRowTests(_DirectionTestCase):

    def test_customer_facing_verb_kept_on_a_real_email_row(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _public_email('We have an update on your lost item search.',
                                  '2026-09-18T17:15:00Z')]
        good = 'We emailed the customer a photo of the possible match'
        bundle, _ = _capture(dispute, comments, fake_result=_fake_rows({0: good}))
        row = self._row(bundle['timeline'], '12:15')
        self.assertEqual(row['text'], good)
