"""RED-phase spec for round 2 of the Case-timeline work: gaps a code review
and live previews on six real disputes found in the round-1 implementation
(apps/payments/document_service.py). Deliberately unimplemented — these
tests must fail until round 2's behaviour lands, except where noted in the
run report (a handful pin behaviour the round-1 code already gets right by
construction, usually because a fixture that LOOKED unrealistic in round 1
happened to force the correct code path anyway).

Review findings pinned here (R-prefixed):
  R1 Fencing: office names (customer-typed claim data) must reach the
     'dispute_timeline' AI call only via `untrusted`, never `trusted`.
  R2 Office detection: new true/false-positive labels found in production
     ('Macbook Air', 'Covenant' as non-filings; 'Delta'/'JET BLUE'/'Jetblue'/
     'AIR Canada'/'Westjet'/'Seattle'/'Fll'/'Lost and Found Terminal B' and
     marker-only notes as filings).
  R3 Capitalisation: 4+ letter ALL-CAPS words title-case unless a canonical
     brand (JetBlue, WestJet); short (2-3 letter) codes and letter+digit
     tokens (T5/T8) stay as written.
  R4 Numbers: any number an AI row writes, in ANY row kind, must occur as a
     whole number in that row's own source/context/duration/office names,
     or the row falls back — previously only call-duration and a blanket
     filing digit-ban were checked (and nothing at all for email/reply).
  R5 A call-recording summary must attach to exactly the call it actually
     follows, never also to an earlier call it happens to also fall within
     the 15-minute context window of.
  R6 Phone rebuttal: real Zendesk call dicts only ever carry 'answered_by'
     (the handling agent), never 'answered_by_name' — the existing test
     fixture was unrealistic; the "we connected by phone" contradiction
     must be grounded in the recorded fee acceptance, not a dead field.
  R7 Filing-note merge window must measure from the FIRST note in the group,
     not the last, and must break on an intervening call or recorded
     acceptance note, not just a public comment.

Live-preview findings pinned here (P-prefixed) + N1 (the OLDER
'dispute_evidence_narrative' section-sorting AI, not the new timeline
writer):
  P1 A call's AI context must not repeat a linked recorded-acceptance
     note's text (it already gets its own timeline row) — say it was
     recorded and shown separately instead; an AI call line that mentions
     the fee anyway falls back.
  P2 A call's exact formatted length ('15 seconds', '2m 50s') must appear
     literally in its AI context, and the AI's own line must reuse that
     exact wording, not a shorthand ('15s').
  P3 A context-grounded 'voicemail'/'mailbox' claim (the linked summary
     itself says so) must be ALLOWED, not blanket-rejected.
  P4 A trailing period on an AI-written activity is stripped (text and
     activity alike).
  P7 Money in AI text is canonicalised: whole-dollar amounts drop '.00';
     amounts with real cents are left alone.
  P8 An office label that names an airline/airport (not its code) expands
     from the claim's OWN data the same way a code does ('Icelandair' ->
     'Icelandair (FI)', 'United' -> 'United Airlines (UA)', 'Delta' ->
     'Delta Air Lines (DL)' from flight_data); an AI filing row that
     independently writes the same mapped name is accepted, not rejected.
  N1 The (pre-existing) section-sorting AI's own item text for an outbound
     call must say we 'placed' it (plus the call length), never 'NOT
     answered' — the round-1 fix removed the false claim but never added
     'placed'.

Tests build synthetic cases through build_dispute_evidence_bundle(dispute,
embed_attachments=False, use_ai=False|True), the resulting timeline rows,
and (for AI-mode tests) the captured kwargs of the patched
apps.payments.document_service.AIClient.complete call — mirroring
test_report_timeline_rows.py / test_report_timeline_ai.py's fixture and
patch style, but with fresh, self-contained fixtures (unique alf_claim_id /
paypal_dispute_id per case).

Run:
    ( cd /Users/alex/Code/proj-alf/alfapp/.worktrees/tl && ../../.venv/bin/python -m pytest \\
      apps/payments/tests/test_report_timeline_round2.py -o addopts="" -q -p no:cacheprovider )
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
# Shared fixture builders — mirrors test_report_timeline_rows.py's /
# test_report_timeline_ai.py's style, but every claim/dispute gets its own
# auto-incrementing unique id so no test has to hand-pick one.
# ---------------------------------------------------------------------------

_SEQ = [0]


def _next_id():
    _SEQ[0] += 1
    return _SEQ[0]


def _claim(**kw):
    n = _next_id()
    base = dict(
        alf_claim_id=f'ALF-RT2-{n:04d}', client_name='Test Client',
        client_email=f'client-rt2-{n}@example.com', price_paid=Decimal('75.00'),
        zd_ticket_id=f'ZD-RT2-{n:04d}', flight_details='', flight_data={},
    )
    base.update(kw)
    return Claim.objects.create(**base)


def _dispute(claim, *, created_at=None, **kw):
    n = _next_id()
    base = dict(
        paypal_dispute_id=f'PP-RT2-{n:04d}',
        buyer_email=(claim.client_email if claim else 'buyer@example.com'),
        transaction_id=f'TX-RT2-{n:04d}',
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


def _intake_comment(created_at='2026-09-18T15:12:54Z', reg_id='ALF-RT2-0000',
                    client_name='Test Client', client_email='client@example.com'):
    body = (f'Registration ID: {reg_id}\nName: {client_name}\n'
            f'Email: {client_email} | Phone: +15550000000\n\n'
            'Date/Time: September 18, 2026 7:00 am\n'
            'Airport: George Bush Intercontinental Airport / IAH\n'
            'Airline: United Airlines - UA Flight #: 1226 Seat #:\n\n'
            'Lost object: Carry-On')
    return {'author': {'name': client_name, 'email': client_email}, 'public': False,
            'channel': 'web', 'created_at': created_at, 'body': body, 'attachments': []}


def _agent_note(body, created_at, author_name='Agent One',
                author_email='agent1@alf.example', public=False, with_image=False):
    note = {'author': {'name': author_name, 'email': author_email}, 'public': public,
            'channel': 'web', 'created_at': created_at, 'body': body, 'attachments': []}
    if with_image:
        note['attachments'] = [{'content_type': 'image/png',
                                'content_url': 'https://example.zendesk.com/a.png',
                                'file_name': 'a.png'}]
    return note


def _voice_comment(duration, direction, created_at, answered_by='Agent One', started_at=None):
    return {'author': {'name': answered_by, 'email': 'agent1@alf.example'}, 'public': False,
            'channel': 'voice', 'created_at': created_at,
            'body': f'{direction.title()} call to +15550000000', 'attachments': [],
            'call': {'direction': direction, 'duration': duration, 'answered_by': answered_by,
                     'recorded': True, 'started_at': started_at or created_at,
                     'from_name': 'Airport Lost Found', 'from_phone': '+18310000000',
                     'to_name': 'Test Client', 'to_phone': '+15550000000'}}


def _call_summary_note(created_at, resolution):
    return _agent_note(
        f'**Call Recording Summary**\n\nCaller Name: Test Client\nIssue: Lost item.\n'
        f'Resolution: {resolution}\nNext Steps: Continue the investigation.',
        created_at, author_name='Agent Two', author_email='agent2@alf.example')


# A real recorded-acceptance note shape (mirrors test_report_timeline_rows.py
# / test_dispute_narrative_report.py's own fixtures).
_ACCEPT_TEXT = (
    'The client was called and informed of the call being recorded for quality and training '
    'purposes to what Client agreed and approved.\n\n'
    'At minute  1:40  on our recorded line, Client approved to move forward with a non '
    'refundable fee of $ 75.00 as Client understood our service and agreed to move forward '
    'knowing no guarantees can be provided on lost items.')


def _acceptance_note(created_at):
    return _agent_note(_ACCEPT_TEXT, created_at)


def _public_email(body, created_at, author_name='Agent One', author_email='agent1@alf.example'):
    return {'author': {'name': author_name, 'email': author_email}, 'public': True,
            'channel': 'email', 'created_at': created_at, 'body': body, 'attachments': []}


def _bundle(dispute, comments, use_ai=False):
    with patch.object(ds, '_fetch_zendesk_ticket_full',
                      return_value={'ticket': {'id': dispute.zd_ticket_id}, 'comments': comments}):
        return ds.build_dispute_evidence_bundle(dispute, embed_attachments=False, use_ai=use_ai)


def _fake_rows(mapping):
    """A duck-typed TimelineActivities stand-in: {index: activity} -> an
    object with a .rows list of SimpleNamespace(index=int, activity=str)."""
    return types.SimpleNamespace(
        rows=[types.SimpleNamespace(index=i, activity=a) for i, a in mapping.items()])


def _capture(dispute, comments, target_site, fake_result=None, use_ai=True):
    """Build the bundle with the AI wired on, capturing the kwargs of the
    call to `target_site` and (optionally) returning `fake_result` for it.
    Every OTHER call_site raises — safe everywhere in this module:
    _narrate_evidence and _narrate_timeline both catch any exception from
    AIClient.complete, and _narrate_image_evidence is never reached at all
    once _narrate_evidence runs (every fixture below keeps at least one
    non-image-only item — a call or a public email/reply — alongside any
    image-only filing note, so text_items is always non-empty and
    _narrate_evidence always runs first)."""
    captured = {}

    def _side_effect(**kwargs):
        site = kwargs.get('call_site')
        if site == target_site:
            captured.update(kwargs)
            if fake_result is not None:
                if isinstance(fake_result, BaseException):
                    raise fake_result
                return fake_result
            if site == 'dispute_timeline':
                from apps.ai.schemas import TimelineActivities
                return TimelineActivities(rows=[])
            return types.SimpleNamespace(items=[])
        raise Exception(f"unexpected call_site {site!r} in this test")

    with patch.object(ds, '_fetch_zendesk_ticket_full',
                      return_value={'ticket': {'id': dispute.zd_ticket_id}, 'comments': comments}), \
         patch('apps.payments.document_service.AIClient.complete', side_effect=_side_effect):
        bundle = ds.build_dispute_evidence_bundle(dispute, embed_attachments=False, use_ai=use_ai)
    return bundle, captured


class _AICase(TestCase):
    """Shared setUp for every AI-mode test in this file."""

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.save()


# ---------------------------------------------------------------------------
# R1 — office names are customer-typed claim data: UNTRUSTED, never trusted.
# ---------------------------------------------------------------------------

class OfficeNameFencingTests(_AICase):
    def test_office_names_reach_the_ai_only_as_untrusted(self):
        claim = _claim(flight_details='Airport: George Bush Intercontinental Airport / IAH')
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _voice_comment(60, 'outbound', '2026-09-18T16:00:00Z'),
                    _agent_note('IAH', '2026-09-18T17:07:21Z', with_image=True)]
        bundle, captured = _capture(dispute, comments, 'dispute_timeline')
        self.assertIn('trusted', captured)
        self.assertIn('untrusted', captured)
        trusted_str = str(captured['trusted'])
        untrusted_str = str(captured['untrusted'])
        self.assertNotIn(
            'George Bush Intercontinental Airport', trusted_str,
            "the office name map is customer-typed claim data — it must never reach the AI "
            "as trusted (prompt-injection fencing)")
        self.assertIn('George Bush Intercontinental Airport', untrusted_str)


# ---------------------------------------------------------------------------
# R2 — new true/false-positive office labels found in production.
# ---------------------------------------------------------------------------

class OfficeDetectionTests(TestCase):
    def _note_row(self, claim_kw, first_line, *, with_image=True, expect_filing):
        claim = _claim(**claim_kw)
        dispute = _dispute(claim)
        note = _agent_note(first_line, '2026-09-18T17:07:21Z', with_image=with_image)
        bundle = _bundle(dispute, [_intake_comment(), note])
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        if expect_filing:
            self.assertEqual(len(rows), 1, bundle['timeline'])
            return rows[0]
        self.assertEqual(rows, [], bundle['timeline'])
        return None

    def test_macbook_air_and_covenant_are_not_filings(self):
        for label in ('Macbook Air', 'Covenant'):
            with self.subTest(label=label):
                self._note_row({}, label, expect_filing=False)

    def test_delta_is_a_filing_even_with_no_delta_claim_data(self):
        row = self._note_row({}, 'Delta', expect_filing=True)
        self.assertEqual(row['text'],
                         'Lost-item information was submitted through Delta channels')

    def test_jetblue_variants_canonicalise(self):
        for label in ('JET BLUE', 'Jetblue'):
            with self.subTest(label=label):
                row = self._note_row({}, label, expect_filing=True)
                self.assertIn('JetBlue', row['text'])
                self.assertNotIn(label, row['text'])

    def test_air_canada_capitalisation(self):
        row = self._note_row({}, 'AIR Canada', expect_filing=True)
        self.assertIn('Air Canada', row['text'])
        self.assertNotIn('AIR Canada', row['text'])

    def test_westjet_canonicalises(self):
        row = self._note_row({}, 'Westjet', expect_filing=True)
        self.assertIn('WestJet', row['text'])

    def test_seattle_expands_from_flight_details(self):
        row = self._note_row(
            dict(flight_details='Airport: Seattle-Tacoma International Airport / SEA'),
            'Seattle', expect_filing=True)
        self.assertIn('Seattle-Tacoma International Airport (SEA)', row['text'])

    def test_fll_uppercases(self):
        row = self._note_row({}, 'Fll', expect_filing=True)
        self.assertIn('FLL', row['text'])

    def test_lost_and_found_terminal_b_is_a_filing(self):
        self._note_row({}, 'Lost and Found Terminal B', expect_filing=True)

    def test_report_submitted_marker_is_a_filing_even_without_image(self):
        self._note_row({}, 'Report submitted to the airline lost and found desk, ref #123.',
                       with_image=False, expect_filing=True)

    def test_lost_report_id_marker_is_a_filing_even_without_image(self):
        self._note_row({}, 'Lost report ID: 44821 confirmed with the airline.',
                       with_image=False, expect_filing=True)


# ---------------------------------------------------------------------------
# R3 — capitalisation inside multi-token labels.
# ---------------------------------------------------------------------------

class CapitalisationTests(TestCase):
    def _filing_row(self, first_line):
        claim = _claim()
        dispute = _dispute(claim)
        note = _agent_note(first_line, '2026-09-18T17:07:21Z', with_image=True)
        bundle = _bundle(dispute, [_intake_comment(), note])
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        return rows[0]

    def test_connector_word_lowercased_codes_kept(self):
        row = self._filing_row('JFK T8 AND AA')
        self.assertIn('JFK T8 and AA', row['text'])

    def test_mixed_brand_and_codes_both_kept_correctly(self):
        row = self._filing_row('American Airlines / JFK T8')
        self.assertIn('American Airlines', row['text'])
        self.assertIn('JFK T8', row['text'])


# ---------------------------------------------------------------------------
# R4 — a number an AI row writes, in ANY row kind, must occur as a whole
# number in that row's own source/context/duration/office names.
# ---------------------------------------------------------------------------

class NumberConsistencyTests(_AICase):
    def test_email_fabricated_dollar_amount_falls_back(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _public_email('We confirmed the lost-item details with the airline today.',
                                  '2026-09-18T17:15:00Z')]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows(
                {0: 'We emailed the customer confirming a $9,999 valuation for the lost item'}))
        rows = [e for e in bundle['timeline'] if 'emailed' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'], 'We emailed the customer an update on their case')

    def test_call_fabricated_gate_number_falls_back(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(), _voice_comment(170, 'outbound', '2026-09-18T16:54:58Z')]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows({0: 'We called the customer (2m 50s) about gate 22 information'}))
        rows = [e for e in bundle['timeline'] if 'called' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'], 'We called the customer (2m 50s)')

    def test_email_number_present_in_source_is_kept(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _public_email('We have submitted your claim reference F1853 to the airline.',
                                  '2026-09-18T17:15:00Z')]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows(
                {0: 'We emailed the customer confirming flight F1853 was logged with the airline'}))
        rows = [e for e in bundle['timeline'] if 'emailed' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'],
                         'We emailed the customer confirming flight F1853 was logged with the airline')

    def test_email_truncated_number_falls_back(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _public_email('Reference number 1853 was provided to the airline.',
                                  '2026-09-18T17:15:00Z')]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows(
                {0: 'We emailed the customer with reference 185 for follow-up'}))
        rows = [e for e in bundle['timeline'] if 'emailed' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'], 'We emailed the customer an update on their case')


# ---------------------------------------------------------------------------
# R5 — a call-recording summary attaches to exactly the call it actually
# follows, never also to an earlier call within the same 15-minute window.
# ---------------------------------------------------------------------------

class OneSummaryPerCallTests(_AICase):
    def test_summary_attaches_to_the_later_call_only(self):
        claim = _claim()
        dispute = _dispute(claim)
        phrase = 'The shipment details were confirmed with the caller.'
        comments = [
            _intake_comment(),
            _voice_comment(60, 'outbound', '2026-09-18T16:00:00Z'),   # call 1
            _voice_comment(45, 'outbound', '2026-09-18T16:10:00Z'),   # call 2, 10 min later
            _call_summary_note('2026-09-18T16:12:00Z', phrase),       # 2 min after call 2
        ]
        bundle, captured = _capture(dispute, comments, 'dispute_timeline')
        records = captured['untrusted']['zendesk_comment']
        self.assertEqual(len(records), 2, records)
        self.assertNotIn(phrase, records[0], "must not attach to the EARLIER call")
        self.assertIn(phrase, records[1], "must attach to the call it actually follows")
        joined = ' '.join(records)
        self.assertEqual(joined.count(phrase), 1,
                         "the summary text must appear exactly once across the untrusted input")


# ---------------------------------------------------------------------------
# R6 — a real call dict only ever carries 'answered_by' (the AGENT), on
# every call regardless of whether the customer picked up; that alone must
# never assert the 'connected by phone' contradiction — only a recorded fee
# acceptance proves it. (The companion existing test was updated in
# test_dispute_narrative_report.py; see the run report.)
# ---------------------------------------------------------------------------

class PhoneRebuttalRealisticCallTests(TestCase):
    def test_answered_by_alone_without_acceptance_note_does_not_assert_connection(self):
        claim = _claim()
        dispute = _dispute(claim, dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
        comments = [{'channel': 'voice', 'call': {'duration': 120, 'answered_by': 'Mark'}}]
        point = ds._claims_response(dispute, comments, claim, {})['points'][0]
        self.assertNotIn('connected with the customer by phone', point)
        self.assertIn('do not dispute', point)


# ---------------------------------------------------------------------------
# R7 — the filing-merge window is measured from the FIRST note in an open
# group, not the last; a phone call or recorded-acceptance note in between
# breaks the merge, same as a public comment already does.
# ---------------------------------------------------------------------------

class FilingMergeWindowTests(TestCase):
    def _filing_note(self, label, when):
        return _agent_note(label, when, with_image=True)

    def test_window_measured_from_the_first_note_not_the_last(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [
            _intake_comment(),
            self._filing_note('AAA', '2026-09-18T17:30:00Z'),
            self._filing_note('BBB', '2026-09-18T18:25:00Z'),   # 55 min after AAA
            self._filing_note('CCC', '2026-09-18T19:20:00Z'),   # 55 after BBB, 110 after AAA
        ]
        bundle = _bundle(dispute, comments)
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 2, bundle['timeline'])
        self.assertIn('AAA and BBB', rows[0]['text'])
        self.assertEqual(rows[0]['when'], 'Sep 18, 2026 12:30')
        self.assertIn('CCC', rows[1]['text'])
        self.assertNotIn('AAA', rows[1]['text'])

    def test_call_between_filings_breaks_the_merge(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [
            _intake_comment(),
            self._filing_note('AAA', '2026-09-18T17:30:00Z'),
            _voice_comment(30, 'outbound', '2026-09-18T17:32:00Z'),
            self._filing_note('BBB', '2026-09-18T17:35:00Z'),
        ]
        bundle = _bundle(dispute, comments)
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 2, bundle['timeline'])

    def test_recorded_acceptance_note_between_filings_breaks_the_merge(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [
            _intake_comment(),
            self._filing_note('AAA', '2026-09-18T17:30:00Z'),
            _acceptance_note('2026-09-18T17:32:00Z'),
            self._filing_note('BBB', '2026-09-18T17:35:00Z'),
        ]
        bundle = _bundle(dispute, comments)
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 2, bundle['timeline'])


# ---------------------------------------------------------------------------
# P1 — a call's AI context must not repeat a linked recorded-acceptance
# note's text (it already gets its own fixed timeline row); an AI call line
# that mentions the fee anyway falls back.
# ---------------------------------------------------------------------------

class NoFeeRepetitionTests(_AICase):
    def _comments(self):
        return [_intake_comment(),
                _voice_comment(170, 'outbound', '2026-09-18T16:54:58Z'),
                _acceptance_note('2026-09-18T16:56:10Z')]   # ~1m12s after the call

    def test_untrusted_call_context_does_not_repeat_the_fee_text(self):
        claim = _claim()
        dispute = _dispute(claim)
        bundle, captured = _capture(dispute, self._comments(), 'dispute_timeline')
        records = captured['untrusted']['zendesk_comment']
        self.assertEqual(len(records), 1, records)
        call_record = records[0]
        self.assertNotIn('non refundable fee of $ 75.00', call_record)
        self.assertNotIn('75.00', call_record)
        low = call_record.lower()
        self.assertTrue('shown separately' in low or 'own row' in low,
                        f"expected 'shown separately' or 'own row' in: {call_record!r}")

    def test_ai_call_line_mentioning_the_fee_falls_back(self):
        claim = _claim()
        dispute = _dispute(claim)
        bundle, _ = _capture(
            dispute, self._comments(), 'dispute_timeline',
            fake_result=_fake_rows(
                {0: 'We called the customer (2m 50s) to discuss the non-refundable service fee'}))
        rows = [e for e in bundle['timeline'] if 'called' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'], 'We called the customer (2m 50s)')


# ---------------------------------------------------------------------------
# P2 — a call's exact formatted length must appear literally in its AI
# context, and the AI's own line must reuse that exact wording.
# ---------------------------------------------------------------------------

class ExactCallLengthTests(_AICase):
    def test_untrusted_context_contains_the_exact_parenthesised_length(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(), _voice_comment(15, 'outbound', '2026-09-18T16:54:58Z')]
        bundle, captured = _capture(dispute, comments, 'dispute_timeline')
        records = captured['untrusted']['zendesk_comment']
        self.assertEqual(len(records), 1, records)
        self.assertIn('(15 seconds)', records[0])

    def test_shorthand_duration_falls_back(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(), _voice_comment(15, 'outbound', '2026-09-18T16:54:58Z')]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows({0: 'We called the customer (15s) to confirm the details'}))
        rows = [e for e in bundle['timeline'] if 'called' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'], 'We called the customer (15 seconds)')

    def test_exact_minutes_seconds_wording_is_kept(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(), _voice_comment(170, 'outbound', '2026-09-18T16:54:58Z')]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows(
                {0: 'We called the customer (2m 50s) to confirm the lost-item details'}))
        rows = [e for e in bundle['timeline'] if 'called' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'],
                         'We called the customer (2m 50s) to confirm the lost-item details')


# ---------------------------------------------------------------------------
# P3 — a context-grounded 'voicemail'/'mailbox' claim (the linked summary
# itself says so) must be ALLOWED, not blanket-rejected.
# ---------------------------------------------------------------------------

class VoicemailFromContextTests(_AICase):
    def test_voicemail_wording_kept_when_the_linked_summary_supports_it(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [
            _intake_comment(),
            _voice_comment(27, 'outbound', '2026-09-18T16:54:58Z'),
            _call_summary_note('2026-09-18T16:56:00Z',
                               'The call is not able to leave a voicemail due to the full mailbox.'),
        ]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows(
                {0: 'We called the customer (27 seconds) but could not leave a voicemail because '
                    'the mailbox was full'}))
        rows = [e for e in bundle['timeline'] if 'called' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'],
                         'We called the customer (27 seconds) but could not leave a voicemail '
                         'because the mailbox was full')


# ---------------------------------------------------------------------------
# P4 — a trailing period on an AI-written activity is stripped, in both
# 'text' and 'activity'.
# ---------------------------------------------------------------------------

class TrailingPeriodTests(_AICase):
    def test_trailing_period_removed_from_text_and_activity(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _public_email('We are continuing to search for the lost item.',
                                  '2026-09-18T17:15:00Z')]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows(
                {0: 'We emailed the customer with an update on their filed report.'}))
        rows = [e for e in bundle['timeline'] if 'emailed' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        row = rows[0]
        self.assertFalse(row['text'].endswith('.'), row['text'])
        self.assertFalse(str(row['activity']).endswith('.'), str(row['activity']))
        self.assertEqual(row['text'], 'We emailed the customer with an update on their filed report')


# ---------------------------------------------------------------------------
# P7 — money in AI text is canonicalised: whole-dollar amounts drop '.00';
# amounts with real cents are left alone.
# ---------------------------------------------------------------------------

class MoneyFormattingTests(_AICase):
    def test_whole_dollar_amount_drops_trailing_zero_cents(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _public_email('The $45.00 service fee was charged as agreed.',
                                  '2026-09-18T17:15:00Z')]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows(
                {0: 'We emailed the customer confirming the $45.00 service fee was charged'}))
        rows = [e for e in bundle['timeline'] if 'emailed' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'],
                         'We emailed the customer confirming the $45 service fee was charged')

    def test_amount_with_real_cents_is_unchanged(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(),
                    _public_email('The $45.50 service fee was charged as agreed.',
                                  '2026-09-18T17:15:00Z')]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows(
                {0: 'We emailed the customer confirming the $45.50 service fee was charged'}))
        rows = [e for e in bundle['timeline'] if 'emailed' in e['text'].lower()]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'],
                         'We emailed the customer confirming the $45.50 service fee was charged')


# ---------------------------------------------------------------------------
# P8 — an office label that names an airline/airport (not its code) expands
# from the claim's OWN data the same way a code does.
# ---------------------------------------------------------------------------

class NameExpansionTests(TestCase):
    def _filing_row(self, claim_kw, first_line):
        claim = _claim(**claim_kw)
        dispute = _dispute(claim)
        note = _agent_note(first_line, '2026-09-18T17:07:21Z', with_image=True)
        bundle = _bundle(dispute, [_intake_comment(), note])
        rows = [e for e in bundle['timeline'] if 'submitted through' in e['text']]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        return rows[0]

    def test_icelandair_name_expands_to_name_and_code(self):
        row = self._filing_row(dict(flight_details='Airline: Icelandair - FI'), 'Icelandair')
        self.assertIn('Icelandair (FI)', row['text'])

    def test_united_partial_name_expands_to_full_name_and_code(self):
        row = self._filing_row(dict(flight_details='Airline: United Airlines - UA'), 'United')
        self.assertIn('United Airlines (UA)', row['text'])

    def test_delta_name_expands_from_flight_data(self):
        row = self._filing_row(
            dict(flight_data={'airline': 'Delta Air Lines', 'number': 'DL2887', 'legs': []}),
            'Delta')
        self.assertIn('Delta Air Lines (DL)', row['text'])


class NameExpansionAIModeTests(_AICase):
    def test_ai_filing_row_using_the_mapped_name_is_accepted(self):
        claim = _claim(flight_data={'airline': 'Delta Air Lines', 'number': 'DL2887', 'legs': []})
        dispute = _dispute(claim)
        comments = [
            _intake_comment(),
            _voice_comment(60, 'outbound', '2026-09-18T16:00:00Z'),
            _agent_note('Delta', '2026-09-18T17:07:21Z', with_image=True),
        ]
        bundle, _ = _capture(
            dispute, comments, 'dispute_timeline',
            fake_result=_fake_rows({0: 'We called the customer (1m 0s)',
                                    1: 'Delta Air Lines (DL)'}))
        # Once the AI text is accepted it REPLACES the deterministic
        # 'submitted through' prefix, so the filing row can no longer be
        # found by that substring — it is found by its own timestamp
        # instead (the filing note's own created_at, 17:07:21Z).
        rows = [e for e in bundle['timeline'] if '12:07' in e['when']]
        self.assertEqual(len(rows), 1, bundle['timeline'])
        self.assertEqual(rows[0]['text'], 'Delta Air Lines (DL)')


# ---------------------------------------------------------------------------
# N1 — the OLDER 'dispute_evidence_narrative' section-sorting AI's own item
# text for an outbound call must say we 'placed' it (plus the call length),
# never 'NOT answered'.
# ---------------------------------------------------------------------------

class SectionSortingCallWordingTests(_AICase):
    def test_outbound_call_item_text_says_placed_and_the_length(self):
        claim = _claim()
        dispute = _dispute(claim)
        comments = [_intake_comment(), _voice_comment(170, 'outbound', '2026-09-18T16:54:58Z')]
        bundle, captured = _capture(dispute, comments, 'dispute_evidence_narrative')
        records = captured['untrusted']['zendesk_comment']
        call_record = next(r for r in records if 'call' in r.lower())
        self.assertIn('placed', call_record.lower())
        self.assertIn('2m 50s', call_record)
        self.assertNotIn('NOT answered', call_record)
