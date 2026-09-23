"""RED-phase spec for de-duplicating identical internal-note rows on the
Case timeline (apps/payments/document_service.py). Deliberately
unimplemented — most of these tests must fail until the behaviour lands; a
few sub-cases (3, 4, 5a/b/c) pin behaviour the current code ALREADY gets
right (nothing currently merges any timeline row of any kind, at any
distance) and are kept as a control/regression guard around the new
de-duplication check, so the fix can't over-collapse real repeated steps —
see the run report for exactly which is which.

WHY: on a real dispute the team logged the same airport notice ("We found a
match! ... Orlando International Airport (MCO) has found an item that
matches your lost item ...") as two internal notes 19 minutes apart, so the
Case timeline showed two identical rows.

`_timeline_record_rows` (apps/payments/document_service.py) currently gives
EVERY substantive internal update note (`_is_substantial_internal_update`)
its own timeline row, unconditionally — there is no merge/de-dup step for
'update' rows at all (unlike 'filing' notes, which already merge within
`_FILING_MERGE_WINDOW` via `_timeline_record_rows`'s `pending`/
`_flush_pending` machinery, and unlike 'call'/'email'/'reply' rows, which
never merge and never should). So when two internal notes carry the same
substance close together, and the AI (patched here) writes the same
activity sentence for both — a realistic outcome, since `_narrate_timeline`
feeds each row's own comment body as context and near-identical bodies
produce near-identical sentences — both notes surface as separate rows
today.

Pinned here (dispute team's own numbering):
  1. Two same-body notes 19 minutes apart, AI writes the identical activity
     for both -> exactly ONE row, at the FIRST note's own time.
  2. Same, but the AI's two sentences differ only in case, whitespace or a
     trailing period -> still one row (a normalize-before-compare check).
  3. Same, but 30 hours apart -> two rows (a day-plus gap is a new event,
     not a duplicate) — CONTROL: already true today, since nothing
     currently merges 'update' rows at any distance.
  4. Two notes 10 minutes apart with genuinely DIFFERENT AI activities -> two
     rows — CONTROL: already true today.
  5. Must not over-collapse real repeats of a DIFFERENT row kind: (a) two
     outbound calls 5 minutes apart both rendering the identical sentence ->
     two rows; (b) two of our emails on different days with identical AI
     activity -> two rows; (c) two of our emails 10 minutes apart with
     identical AI activity -> two rows (a real send is a real send) — ALL
     THREE are CONTROL: 'call' and 'email' rows have no merge step either,
     today or after this fix (only 'update' rows should ever collapse).
  6. Rows between the duplicates don't matter for the merge: note A, then a
     customer reply, then an identical note A 15 minutes after the FIRST ->
     one note row plus the reply row (the intervening different-kind row
     does not block the note/note merge, and is itself untouched).

Tests build synthetic cases through build_dispute_evidence_bundle(dispute,
embed_attachments=False, use_ai=True), mirroring
test_report_timeline_direction.py's / test_report_timeline_ai.py's fixture
and AIClient.complete patching for call_site='dispute_timeline' — fresh,
self-contained fixtures (unique alf_claim_id / paypal_dispute_id per case,
all under an 'RTU' prefix — Report Timeline dUplicates — distinct from every
other dispute test file so parallel test runs never collide).

Run:
    ( cd /Users/alex/Code/proj-alf/alfapp/.worktrees/tl && ../../.venv/bin/python -m pytest \\
      apps/payments/tests/test_report_timeline_duplicates.py -o addopts="" -q -p no:cacheprovider )
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
# Shared fixture builders — mirrors test_report_timeline_direction.py's
# style: every claim/dispute gets its own auto-incrementing unique id, under
# a prefix ('RTU') no other dispute test file uses.
# ---------------------------------------------------------------------------

_SEQ = [0]


def _next_id():
    _SEQ[0] += 1
    return _SEQ[0]


def _claim(**kw):
    n = _next_id()
    base = dict(
        alf_claim_id=f'ALF-RTU-{n:04d}', client_name='Test Client',
        client_email=f'client-rtu-{n}@example.com', price_paid=Decimal('75.00'),
        zd_ticket_id=f'ZD-RTU-{n:04d}', flight_details='', flight_data={},
    )
    base.update(kw)
    return Claim.objects.create(**base)


def _dispute(claim, *, created_at=None, **kw):
    n = _next_id()
    base = dict(
        paypal_dispute_id=f'PP-RTU-{n:04d}',
        buyer_email=(claim.client_email if claim else 'buyer@example.com'),
        transaction_id=f'TX-RTU-{n:04d}',
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


def _intake_comment(created_at='2026-09-18T15:12:54Z', reg_id='ALF-RTU-0000',
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


def _public_email(body, created_at, author_name='Agent One', author_email='agent1@alf.example'):
    return {'author': {'name': author_name, 'email': author_email}, 'public': True,
            'channel': 'email', 'created_at': created_at, 'body': body, 'attachments': []}


def _customer_reply(body, created_at, client_name, client_email):
    return {'author': {'name': client_name, 'email': client_email}, 'public': True,
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
    test_report_timeline_round2.py's / test_report_timeline_direction.py's
    `_capture`: `_narrate_evidence` catches any exception from
    AIClient.complete and returns None, so `_narrate_image_evidence` is
    never reached (every fixture below keeps at least one non-image-only
    item — a call or a note/email with real text — so text_items is always
    non-empty and `_narrate_evidence` always runs first)."""
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


class _DupTestCase(TestCase):
    """Shared setUp (AI key, so the timeline writer actually calls the AI) +
    row-lookup helpers."""

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.save()

    def _rows_at(self, timeline, when_contains):
        return [e for e in timeline if when_contains in e['when']]

    def _rows_with_text(self, timeline, text):
        return [e for e in timeline if e['text'] == text]


# The exact preview note: an agent logging an airport's match notice
# internally (never sent anywhere as written) — reused verbatim from
# test_report_timeline_direction.py's InternalUpdateNoteMessagingTests.
NOTE_BODY = ('We found a match!\n\nHi Test Client,\nWe have great news, Orlando '
            'International Airport (MCO) has found an item that matches your lost '
            'item.\nPlease review the match and confirm it is yours.')

MATCH_TEXT = 'Orlando International Airport (MCO) reported a possible match for the lost item'


# ---------------------------------------------------------------------------
# 1/2 — identical (or near-identical) AI text, close together, must collapse.
# ---------------------------------------------------------------------------

class DuplicateInternalNoteCollapseTests(_DupTestCase):

    def test_1_identical_ai_text_19_minutes_apart_collapses_to_one_row(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _agent_note(NOTE_BODY, '2026-09-18T19:00:00Z'),
                    _agent_note(NOTE_BODY, '2026-09-18T19:19:00Z')]
        fake = _fake_rows({0: MATCH_TEXT, 1: MATCH_TEXT})
        bundle, _ = _capture(dispute, comments, fake_result=fake)
        tl = bundle['timeline']

        matches = self._rows_with_text(tl, MATCH_TEXT)
        self.assertEqual(
            len(matches), 1,
            f"two internal notes with the identical AI-written activity, 19 minutes "
            f"apart, must collapse to a single row: {[e['when'] for e in tl]!r}")
        self.assertTrue(
            matches[0]['when'].endswith('14:00'),
            f"the surviving row must sit at the FIRST note's own time, got "
            f"{matches[0]['when']!r}")
        self.assertEqual(
            self._rows_at(tl, '14:19'), [],
            "no separate row should remain at the second (duplicate) note's time")

    def test_2_case_whitespace_and_trailing_period_differences_still_collapse(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _agent_note(NOTE_BODY, '2026-09-18T19:00:00Z'),
                    _agent_note(NOTE_BODY, '2026-09-18T19:19:00Z')]
        # Same substance as MATCH_TEXT, but upper-cased, with an internal
        # double space, and padded/terminated so the only *meaningful*
        # difference left after _clean_ai_text's own strip()/period-drop is
        # case + internal whitespace.
        variant = ('  ' + MATCH_TEXT.upper().replace('POSSIBLE MATCH', 'POSSIBLE  MATCH')
                  + '.  ')
        fake = _fake_rows({0: MATCH_TEXT, 1: variant})
        bundle, _ = _capture(dispute, comments, fake_result=fake)
        tl = bundle['timeline']

        self.assertEqual(
            len(self._rows_at(tl, '14:00')), 1,
            f"exactly one row must survive at the first note's time: "
            f"{[e['when'] for e in tl]!r}")
        self.assertEqual(
            self._rows_at(tl, '14:19'), [],
            "a duplicate that differs only in case/whitespace/trailing period must "
            "still collapse into the first row, not survive as a second one")
        survivors = [e for e in tl if 'possible match' in e['text'].lower()]
        self.assertEqual(
            len(survivors), 1,
            f"only one row total should carry this note's substance: {survivors!r}")


# ---------------------------------------------------------------------------
# 3/4 — must NOT collapse a genuinely later event or a genuinely different
# activity. CONTROL cases: already true today (nothing merges 'update' rows
# at all yet), and must stay true once the merge lands.
# ---------------------------------------------------------------------------

class DuplicateInternalNoteNonCollapseTests(_DupTestCase):

    def test_3_same_text_thirty_hours_apart_does_not_collapse(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _agent_note(NOTE_BODY, '2026-09-18T19:00:00Z'),
                    _agent_note(NOTE_BODY, '2026-09-20T01:00:00Z')]  # +30h
        fake = _fake_rows({0: MATCH_TEXT, 1: MATCH_TEXT})
        bundle, _ = _capture(dispute, comments, fake_result=fake)
        tl = bundle['timeline']

        first = self._rows_at(tl, '14:00')
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['text'], MATCH_TEXT)

        later = [e for e in tl if 'Sep 19' in e['when'] and '20:00' in e['when']]
        self.assertEqual(
            len(later), 1,
            f"a day-plus gap is a new event, not a duplicate — expected a second "
            f"row: {[e['when'] for e in tl]!r}")
        self.assertEqual(later[0]['text'], MATCH_TEXT)

    def test_4_different_ai_text_ten_minutes_apart_does_not_collapse(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _agent_note(NOTE_BODY, '2026-09-18T19:00:00Z'),
                    _agent_note('A completely different internal note about a separate '
                                'matter on this case, long enough to be substantive.',
                                '2026-09-18T19:10:00Z')]
        other_text = 'A separate matter was logged for the case file'
        fake = _fake_rows({0: MATCH_TEXT, 1: other_text})
        bundle, _ = _capture(dispute, comments, fake_result=fake)
        tl = bundle['timeline']

        row1 = self._rows_at(tl, '14:00')
        self.assertEqual(len(row1), 1)
        self.assertEqual(row1[0]['text'], MATCH_TEXT)

        row2 = self._rows_at(tl, '14:10')
        self.assertEqual(
            len(row2), 1,
            f"two notes with genuinely different AI text must both keep their own "
            f"row: {[e['when'] for e in tl]!r}")
        self.assertEqual(row2[0]['text'], other_text)


# ---------------------------------------------------------------------------
# 5 — real repeated steps of a DIFFERENT row kind must never be collapsed.
# CONTROL cases: 'call'/'email' rows have no merge step, today or after this
# fix — only 'update' rows should ever merge.
# ---------------------------------------------------------------------------

class RealRepeatsOfOtherRowKindsNeverCollapseTests(_DupTestCase):

    def test_5a_two_outbound_calls_five_minutes_apart_with_identical_text_stay_separate(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _voice_comment(13, 'outbound', '2026-09-18T16:54:58Z'),
                    _voice_comment(13, 'outbound', '2026-09-18T16:59:58Z')]
        call_text = 'We called the customer (13 seconds)'
        fake = _fake_rows({0: call_text, 1: call_text})
        bundle, _ = _capture(dispute, comments, fake_result=fake)
        tl = bundle['timeline']

        matches = self._rows_with_text(tl, call_text)
        self.assertEqual(
            len(matches), 2,
            f"two real, distinct calls that both render the identical sentence are "
            f"not duplicates — only 'update' rows should ever merge: "
            f"{[e['when'] for e in tl]!r}")
        self.assertEqual(len(self._rows_at(tl, '11:54')), 1)
        self.assertEqual(len(self._rows_at(tl, '11:59')), 1)

    def test_5b_two_emails_on_different_days_with_identical_text_stay_separate(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _public_email('We have an update on your lost item search.',
                                  '2026-09-18T17:15:00Z'),
                    _public_email('We have an update on your lost item search.',
                                  '2026-09-19T17:15:00Z')]
        email_text = 'We emailed the customer with an update on the search for their lost item'
        fake = _fake_rows({0: email_text, 1: email_text})
        bundle, _ = _capture(dispute, comments, fake_result=fake)
        tl = bundle['timeline']

        matches = self._rows_with_text(tl, email_text)
        self.assertEqual(
            len(matches), 2,
            f"two of our own emails, days apart, are real sends — never merged: "
            f"{[e['when'] for e in tl]!r}")

    def test_5c_two_emails_ten_minutes_apart_with_identical_text_stay_separate(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _public_email('We have an update on your lost item search.',
                                  '2026-09-18T17:15:00Z'),
                    _public_email('We have an update on your lost item search.',
                                  '2026-09-18T17:25:00Z')]
        email_text = 'We emailed the customer with an update on the search for their lost item'
        fake = _fake_rows({0: email_text, 1: email_text})
        bundle, _ = _capture(dispute, comments, fake_result=fake)
        tl = bundle['timeline']

        matches = self._rows_with_text(tl, email_text)
        self.assertEqual(
            len(matches), 2,
            f"two of our own emails, ten minutes apart, are real sends — never "
            f"merged: {[e['when'] for e in tl]!r}")
        self.assertEqual(len(self._rows_at(tl, '12:15')), 1)
        self.assertEqual(len(self._rows_at(tl, '12:25')), 1)


# ---------------------------------------------------------------------------
# 6 — an intervening row of a different kind must not block the note/note
# merge, and must itself be left untouched.
# ---------------------------------------------------------------------------

class InterveningRowDoesNotBlockNoteMergeTests(_DupTestCase):

    def test_6_reply_between_duplicate_notes_does_not_block_the_merge(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _agent_note(NOTE_BODY, '2026-09-18T19:00:00Z'),
                    _customer_reply('Any updates on my sunglasses?', '2026-09-18T19:07:00Z',
                                    claim.client_name, claim.client_email),
                    _agent_note(NOTE_BODY, '2026-09-18T19:15:00Z')]  # +15 min from the FIRST
        reply_text = 'The customer asked for an update on the search'
        fake = _fake_rows({0: MATCH_TEXT, 1: reply_text, 2: MATCH_TEXT})
        bundle, _ = _capture(dispute, comments, fake_result=fake)
        tl = bundle['timeline']

        note_rows = self._rows_with_text(tl, MATCH_TEXT)
        self.assertEqual(
            len(note_rows), 1,
            f"the intervening reply must not block the note/note merge — only one "
            f"note row should survive: {[e['when'] for e in tl]!r}")
        self.assertTrue(note_rows[0]['when'].endswith('14:00'))
        self.assertEqual(
            self._rows_at(tl, '14:15'), [],
            "the second (duplicate) note's own time must carry no row")

        reply_rows = self._rows_with_text(tl, reply_text)
        self.assertEqual(
            len(reply_rows), 1,
            f"the reply itself must be untouched by the note/note merge: "
            f"{[e['when'] for e in tl]!r}")
        self.assertTrue(reply_rows[0]['when'].endswith('14:07'))
