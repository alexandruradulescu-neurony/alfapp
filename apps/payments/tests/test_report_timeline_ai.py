"""RED-phase spec for AI-narrated Case-timeline rows (deliberately unimplemented;
these tests must fail until the behaviour lands).

Intent: each row of the dispute evidence report's "Case timeline" should read as
a specific, descriptive sentence (e.g. 'We called the customer (2m 50s) to
confirm the lost-item details and discuss the investigation process') instead of
a generic label. The SYSTEM keeps control of dates, order and the fixed/factual
rows (claim submitted, PayPal claim filed, recorded acceptance, PayPal
notification); a NEW AI step (AIClient.complete(..., call_site='dispute_timeline',
response_schema=TimelineActivities)) writes the description of record-based rows
only (a qualifying call, our public emails, customer replies, office-filing
rows, and substantive internal update notes) — and every AI-written line is
checked, falling back to a plain deterministic sentence for THAT row alone when
a check fails. This file pins:

  1. The apps.ai.schemas.TimelineActivities/TimelineActivity contract, and the
     general patch-and-call shape of the new 'dispute_timeline' AI call.
  2/3/4. Which comments are eligible and numbered, in chronological order, and
     the exact deterministic + AI-authored text/timestamps for a full case.
  5. The context (call length, call-summary text, office codes, office name
     map) reaching the AI via `trusted`/`untrusted`.
  6. Seven per-row checks (a-g) that force a deterministic fallback for ONE row
     only, leaving the other AI-authored rows untouched.
  7. Clean-ups that KEEP the AI text (dash->comma, unbalanced/excess '**'
     stripped, HTML escaped) rather than rejecting it.
  8. Robustness to a broken/malformed AI reply (raises, None, duplicate/
     out-of-range/omitted indices, or a flood of indices) — the report still
     renders and the fixed rows are never altered.
  9. Substantive internal "update" notes are AI-eligible; a short/near-empty
     one never is; an empty AI answer for one suppresses that row entirely
     (unlike the four archetypal row kinds, which always have a fallback).
  10. Honest provenance: generate_evidence_report labels a document AI-generated
      when only the new timeline writer produced usable content.

Run:
    ( cd /Users/alex/Code/proj-alf/alfapp/.worktrees/tl && ../../.venv/bin/python -m pytest \\
      apps/payments/tests/test_report_timeline_ai.py -o addopts="" -q -p no:cacheprovider )
"""

import types
from datetime import datetime, timezone as dt_tz
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments import document_service as ds
from apps.payments.models import Dispute, DisputeDocument


# ---------------------------------------------------------------------------
# Shared fixture — one case, unique values (distinct from every other dispute
# test file so parallel test runs never collide on paypal_dispute_id etc.).
#
# Claim submitted (intake note)             2026-09-18T15:12:54Z  -> 10:12 CDT
# Buyer's PayPal claim created (payload)     2026-09-18T15:48:29Z  -> 10:48 CDT
# Outbound call, 170s = 2m 50s               2026-09-18T16:54:58Z  -> 11:54 CDT
# '**Call Recording Summary**' (context)     2026-09-18T16:55:09Z  -> 11:55 CDT
# Recorded-acceptance note                   2026-09-18T16:56:10Z  -> 11:56 CDT
# Merged office-filing note (IAH/TSA/UA)     2026-09-18T17:07:21Z  -> 12:07 CDT
# Public email we sent                       2026-09-18T17:15:00Z  -> 12:15 CDT
# Customer reply                             2026-09-18T18:05:00Z  -> 13:05 CDT
# Substantive internal "Update" (MCO)        2026-09-18T19:00:00Z  -> 14:00 CDT
# Short/near-empty internal "Update"         2026-09-18T19:30:00Z  -> 14:30 CDT
# Dispute row created (webhook received)     2026-09-20T14:29:16Z  -> Sep 20 09:29 CDT
# ---------------------------------------------------------------------------

INTAKE_COMMENT = {
    'id': 1001, 'author': {'id': 1, 'name': 'System', 'email': 'system@alf.com'},
    'public': False, 'channel': 'web', 'html_body': '', 'attachments': [], 'call': None,
    'created_at': '2026-09-18T15:12:54Z',
    'body': ('Registration ID: ALF7654321\nName: Test Client\nEmail: client2@example.com\n'
             'Airport: George Bush Intercontinental Airport / IAH\n'
             'Airline: United Airlines - UA Flight #: 1226'),
}

CALL_COMMENT = {
    'id': 1002, 'author': {'id': 2, 'name': 'Agent One', 'email': 'agentone@alf.com'},
    'public': False, 'channel': 'voice', 'body': '', 'html_body': '', 'attachments': [],
    'created_at': '2026-09-18T16:54:58Z',
    'call': {'direction': 'outbound', 'duration': 170, 'answered_by': 'Agent One',
             'answered_by_name': 'Agent One', 'started_at': '2026-09-18T16:54:58Z',
             'recorded': True},
}

CALL_SUMMARY_COMMENT = {
    'id': 1003, 'author': {'id': 3, 'name': 'Agent Two', 'email': 'agenttwo@alf.com'},
    'public': False, 'channel': 'web', 'html_body': '', 'attachments': [], 'call': None,
    'created_at': '2026-09-18T16:55:09Z',
    'body': ('**Call Recording Summary**\n\nCaller Name: Test Client\n'
             'Issue: Lost carry-on luggage.\n'
             'Resolution: Information confirmed with the caller, the team will continue to '
             'track the item, and updates will be sent via email as information becomes '
             'available.\n'
             'Next Steps: The team will proceed with the investigation and keep the customer '
             'informed.'),
}

ACCEPTANCE_COMMENT = {
    'id': 1004, 'author': {'id': 2, 'name': 'Agent One', 'email': 'agentone@alf.com'},
    'public': False, 'channel': 'web', 'html_body': '', 'attachments': [], 'call': None,
    'created_at': '2026-09-18T16:56:10Z',
    'body': ('The client was called and informed of the call being recorded for quality and '
             'training purposes to what Client agreed and approved.\n\n'
             'At minute  1:40  on our recorded line, Client approved to move forward with a '
             'non refundable fee of $ 75.00 as Client understood our service and agreed to '
             'move forward knowing no guarantees can be provided on lost items.'),
}

FILING_COMMENT = {
    'id': 1005, 'author': {'id': 2, 'name': 'Agent One', 'email': 'agentone@alf.com'},
    'public': False, 'channel': 'web', 'html_body': '', 'attachments': [], 'call': None,
    'created_at': '2026-09-18T17:07:21Z',
    'body': ('IAH\n\n ![](https://example.zendesk.com/attachments/token/b1/?name=a.png)\n\n'
             'TSA\n\n ![](https://example.zendesk.com/attachments/token/b2/?name=b.png)\n\n'
             'UA\n\n ![](https://example.zendesk.com/attachments/token/b3/?name=c.png)'),
}

EMAIL_SENT_COMMENT = {
    'id': 1006, 'author': {'id': 2, 'name': 'Agent One', 'email': 'agentone@alf.com'},
    'public': True, 'channel': 'email', 'html_body': '', 'attachments': [], 'call': None,
    'created_at': '2026-09-18T17:15:00Z',
    'body': ('Dear Test Client, we have filed your report with United Airlines and IAH lost '
             'and found and will update you.'),
}

CUSTOMER_REPLY_COMMENT = {
    'id': 1007, 'author': {'id': 9, 'name': 'Test Client', 'email': 'client2@example.com'},
    'public': True, 'channel': 'email', 'html_body': '', 'attachments': [], 'call': None,
    'created_at': '2026-09-18T18:05:00Z',
    'body': 'Thank you for the update, please let me know if you find my sunglasses.',
}

MCO_UPDATE_COMMENT = {
    'id': 1008, 'author': {'id': 2, 'name': 'Agent One', 'email': 'agentone@alf.com'},
    'public': False, 'channel': 'web', 'html_body': '', 'attachments': [], 'call': None,
    'created_at': '2026-09-18T19:00:00Z',
    'body': ('Update\n\nThe MCO lost and found office replied that a pair of sunglasses '
             'matching the description was found and is being held.'),
}

SHORT_UPDATE_COMMENT = {
    'id': 1009, 'author': {'id': 2, 'name': 'Agent One', 'email': 'agentone@alf.com'},
    'public': False, 'channel': 'web', 'html_body': '', 'attachments': [], 'call': None,
    'created_at': '2026-09-18T19:30:00Z',
    'body': 'Update\n![](https://example.zendesk.com/attachments/token/b4/?name=d.png)',
}

# 5 comments -> 2 AI-eligible rows: index 0 = the call, index 1 = the filing.
MINIMAL_COMMENTS = [INTAKE_COMMENT, CALL_COMMENT, CALL_SUMMARY_COMMENT,
                    ACCEPTANCE_COMMENT, FILING_COMMENT]
# 7 comments -> 4 AI-eligible rows: 0=call, 1=filing, 2=email, 3=reply.
FULL_COMMENTS = MINIMAL_COMMENTS + [EMAIL_SENT_COMMENT, CUSTOMER_REPLY_COMMENT]

# The AI's own texts when every check passes (§4/§6 baseline — deliberately
# free of fabricated numbers, staff names, unsupported claims, and under 300
# chars, so only the ONE row a test deliberately breaks can fall back).
GOOD_CALL_TEXT = ('We called the customer (2m 50s) to confirm the lost-item details and '
                  'discuss the investigation process')
GOOD_FILING_TEXT = ('Lost-item information was submitted through **George Bush '
                    'Intercontinental Airport (IAH)**, **TSA**, and **United Airlines (UA)** '
                    'channels')
GOOD_EMAIL_TEXT = ('We emailed the customer confirming their report was filed with United '
                   'Airlines and IAH lost and found')
GOOD_REPLY_TEXT = ('The customer thanked us for the update and asked to be notified once the '
                   'sunglasses are found')
GOOD_MCO_TEXT = ('A pair of sunglasses matching the description was found by MCO lost and '
                 'found and is being held for the customer')

# §6 — the fixed fallback sentence for each row kind, used ONLY when the AI's
# own line for that row fails a check (or the AI is unavailable/off).
FALLBACK_CALL = 'We called the customer (2m 50s)'
FALLBACK_FILING = ('Lost-item information was submitted through George Bush Intercontinental '
                   'Airport (IAH), TSA, and United Airlines (UA) channels')
FALLBACK_EMAIL = 'We emailed the customer an update on their case'
FALLBACK_REPLY = 'The customer replied to us'

# §4 — the four rows that are ALWAYS deterministic (never offered to the AI).
DET_CLAIM_SUBMITTED = 'Lost-item service request ALF7654321 submitted on our website'
DET_PAYPAL_FILED = ('The buyer filed PayPal claim PP-R-TST-000000002, alleging '
                    '“Item not as described”')
DET_ACCEPTANCE = ('During the recorded call, the customer agreed to proceed with the '
                  'non-refundable $75 service fee, understanding that recovery of the lost '
                  'item could not be guaranteed')
DET_NOTIFICATION = 'PayPal case notification received in our internal system'


def _fake_rows(overrides: dict, base: dict | None = None):
    """A duck-typed stand-in for the TimelineActivities the AI would return:
    types.SimpleNamespace(rows=[SimpleNamespace(index=int, activity=str), ...]).
    `base` defaults to the 4-row all-good baseline; `overrides` replaces
    specific indices (so only the row(s) under test can misbehave)."""
    rows = dict(base if base is not None else
               {0: GOOD_CALL_TEXT, 1: GOOD_FILING_TEXT, 2: GOOD_EMAIL_TEXT, 3: GOOD_REPLY_TEXT})
    rows.update(overrides)
    return types.SimpleNamespace(
        rows=[types.SimpleNamespace(index=i, activity=a) for i, a in rows.items()])


class _TimelineAITestCase(TestCase):
    """Shared fixture + patch plumbing for the 'dispute_timeline' AI call."""

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'test-key'
        ss.save()

    def _make_case(self, comments):
        """A fresh Claim + Dispute per the spec's unique fixture, paired with
        the given comment list (a variant of MINIMAL_COMMENTS/FULL_COMMENTS)."""
        claim = Claim.objects.create(
            alf_claim_id='ALF7654321', client_name='Test Client',
            client_email='client2@example.com', price_paid=Decimal('75.00'),
            zd_ticket_id='99002',
            flight_details=('Flight: 1226 | Airline: United Airlines - UA | Airport: George '
                            'Bush Intercontinental Airport / IAH | Date/Time: September 18, '
                            '2026 7:00 am'))
        dispute = Dispute.objects.create(
            paypal_dispute_id='PP-R-TST-000000002', buyer_email='client2@example.com',
            buyer_name='Test Client', transaction_id='TX-99002',
            transaction_date=datetime(2026, 9, 18, 15, 48, 29, tzinfo=dt_tz.utc),
            dispute_reason='MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED',
            dispute_life_cycle_stage='CHARGEBACK', dispute_amount=Decimal('75.00'),
            dispute_currency='USD', zd_ticket_id='99002', claim=claim,
            raw_webhook_payload={'create_time': '2026-09-18T15:48:29.300Z',
                                 'dispute_channel': 'INTERNAL'})
        Dispute.objects.filter(pk=dispute.pk).update(
            created_at=datetime(2026, 9, 20, 14, 29, 16, tzinfo=dt_tz.utc))
        dispute.refresh_from_db()
        return dispute, claim, comments

    def _build_bundle(self, dispute, comments, fake_timeline_result, use_ai=True):
        """Build the evidence bundle with Zendesk fetch + AI faked out (no
        network calls, no real LLM call). `fake_timeline_result` is returned
        for call_site='dispute_timeline' (or raised, if it's an exception
        instance); every OTHER call_site raises, so the section narrator /
        vision harmlessly fall back to None, same as an AI failure for them.
        Returns (bundle, mock_complete) so callers can inspect the calls made."""
        def _side_effect(**kwargs):
            if kwargs.get('call_site') == 'dispute_timeline':
                if isinstance(fake_timeline_result, BaseException):
                    raise fake_timeline_result
                return fake_timeline_result
            raise Exception(f"unexpected call_site {kwargs.get('call_site')!r} in this test")

        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {'id': dispute.zd_ticket_id},
                                       'comments': comments}), \
             patch('apps.payments.document_service.AIClient.complete',
                  side_effect=_side_effect) as mock_complete:
            bundle = ds.build_dispute_evidence_bundle(
                dispute, embed_attachments=False, use_ai=use_ai)
        return bundle, mock_complete

    def _row(self, timeline, when_contains):
        """The single timeline entry whose 'when' contains this substring."""
        matches = [e for e in timeline if when_contains in e['when']]
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one row with when containing {when_contains!r}, "
            f"got {[e['when'] for e in timeline]}")
        return matches[0]

    def _assert_has_shape(self, entry):
        self.assertIn('when', entry)
        self.assertIn('activity', entry)
        self.assertIn('text', entry)


# ---------------------------------------------------------------------------
# 1. The TimelineActivities/TimelineActivity contract + general call shape.
# ---------------------------------------------------------------------------

class TimelineActivitySchemaTests(TestCase):
    """apps.ai.schemas.TimelineActivities / TimelineActivity — the response
    shape for the new call_site='dispute_timeline' AI call."""

    def test_schema_validates_minimal_payload(self):
        from apps.ai.schemas import TimelineActivities, TimelineActivity
        obj = TimelineActivities.model_validate({"rows": [{"index": 0, "activity": "x"}]})
        self.assertEqual(len(obj.rows), 1)
        self.assertIsInstance(obj.rows[0], TimelineActivity)
        self.assertEqual(obj.rows[0].index, 0)
        self.assertEqual(obj.rows[0].activity, "x")


class TimelineUseAIFlagTests(_TimelineAITestCase):
    """use_ai gates the new call, same as every other AI step in this file."""

    def test_use_ai_false_never_calls_the_ai(self):
        dispute, claim, comments = self._make_case(MINIMAL_COMMENTS)
        bundle, mock_complete = self._build_bundle(
            dispute, comments, fake_timeline_result=_fake_rows({}), use_ai=False)
        mock_complete.assert_not_called()

    def test_use_ai_false_still_shows_deterministic_text_for_call_and_filing(self):
        # The call/filing rows are core, always-available rows (they existed
        # before this AI step) — only the *description* is AI-optional.
        dispute, claim, comments = self._make_case(MINIMAL_COMMENTS)
        bundle, _ = self._build_bundle(
            dispute, comments, fake_timeline_result=_fake_rows({}), use_ai=False)
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '11:54')['text'], FALLBACK_CALL)
        self.assertEqual(self._row(tl, '12:07')['text'], FALLBACK_FILING)


# ---------------------------------------------------------------------------
# 2/3/4. Eligible rows, chronological numbering, exact deterministic + AI text.
# ---------------------------------------------------------------------------

class EligibleTimelineRowsTests(_TimelineAITestCase):
    """Which comments are AI-eligible (numbered 0,1,2… chronologically), and
    the exact text + timestamp of every row — AI-authored and deterministic —
    for a realistic case."""

    def test_ai_rows_used_and_deterministic_rows_exact(self):
        dispute, claim, comments = self._make_case(MINIMAL_COMMENTS)
        fake = types.SimpleNamespace(rows=[
            types.SimpleNamespace(
                index=0,
                activity='We called the customer (2m 50s) to confirm the lost-item details '
                         'and discuss the investigation process'),
            types.SimpleNamespace(
                index=1,
                activity='Lost-item information was submitted through **George Bush '
                         'Intercontinental Airport (IAH)**, **TSA**, and **United Airlines '
                         '(UA)** channels'),
        ])
        bundle, _ = self._build_bundle(dispute, comments, fake)
        tl = bundle['timeline']
        for e in tl:
            self._assert_has_shape(e)

        # AI-authored rows, at the call's and the filing's own timestamps.
        call_row = self._row(tl, '11:54')
        self.assertIn('We called the customer (2m 50s) to confirm the lost-item details '
                      'and discuss the investigation process', call_row['text'])
        self.assertNotIn('*', call_row['activity'])  # no markdown in this one -> untouched

        filing_row = self._row(tl, '12:07')
        self.assertIn('<strong>George Bush Intercontinental Airport (IAH)</strong>',
                      filing_row['activity'])
        self.assertIn('<strong>TSA</strong>', filing_row['activity'])
        self.assertIn('<strong>United Airlines (UA)</strong>', filing_row['activity'])
        self.assertNotIn('**', filing_row['activity'])
        self.assertNotIn('**', filing_row['text'])
        self.assertIn('George Bush Intercontinental Airport (IAH)', filing_row['text'])
        self.assertIn('TSA', filing_row['text'])
        self.assertIn('United Airlines (UA)', filing_row['text'])

        # The four ALWAYS-deterministic rows, verbatim.
        self.assertEqual(self._row(tl, '10:12')['text'], DET_CLAIM_SUBMITTED)
        self.assertEqual(self._row(tl, '10:48')['text'], DET_PAYPAL_FILED)
        self.assertEqual(self._row(tl, '11:56')['text'], DET_ACCEPTANCE)
        notification_row = self._row(tl, '09:29')
        self.assertIn('Sep 20', notification_row['when'])
        self.assertEqual(notification_row['text'], DET_NOTIFICATION)

        # Exactly 6 rows: 2 AI-authored + 4 deterministic — nothing invented,
        # nothing dropped.
        self.assertEqual(len(tl), 6)

        # Chronological order, start to finish.
        whens = [e['when'] for e in tl]
        parsed = [datetime.strptime(w, '%b %d, %Y %H:%M') for w in whens]
        self.assertEqual(parsed, sorted(parsed))
        self.assertTrue(whens[0].endswith('10:12'))
        self.assertTrue(whens[-1].endswith('09:29') and 'Sep 20' in whens[-1])

    def test_email_and_reply_rows_are_indices_2_and_3(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        fake = _fake_rows({})  # all 4 good
        bundle, _ = self._build_bundle(dispute, comments, fake)
        tl = bundle['timeline']
        email_row = self._row(tl, '12:15')
        reply_row = self._row(tl, '13:05')
        self.assertEqual(email_row['text'], GOOD_EMAIL_TEXT)
        self.assertEqual(reply_row['text'], GOOD_REPLY_TEXT)
        # 4 AI rows + 4 deterministic rows.
        self.assertEqual(len(tl), 8)


# ---------------------------------------------------------------------------
# 5. Context reaching the AI: call length + summary text + office codes/map.
# ---------------------------------------------------------------------------

class TimelineAIContextTests(_TimelineAITestCase):

    def test_untrusted_carries_call_context_and_office_codes_and_name_map(self):
        dispute, claim, comments = self._make_case(MINIMAL_COMMENTS)
        bundle, mock_complete = self._build_bundle(dispute, comments, _fake_rows({}))
        timeline_calls = [c for c in mock_complete.call_args_list
                          if c.kwargs.get('call_site') == 'dispute_timeline']
        self.assertEqual(len(timeline_calls), 1,
                         "expected exactly one AI call for call_site='dispute_timeline'")
        kwargs = timeline_calls[0].kwargs
        self.assertIn('response_schema', kwargs)
        untrusted_str = str(kwargs.get('untrusted') or {})
        trusted_str = str(kwargs.get('trusted') or {})
        combined = trusted_str + untrusted_str

        self.assertIn('2m 50s', untrusted_str,
                      "the call's exact duration string must reach the AI")
        self.assertIn('Information confirmed with the caller', untrusted_str,
                      "the call-recording-summary context must reach the AI")
        self.assertIn('IAH', untrusted_str)
        self.assertIn('TSA', untrusted_str)
        self.assertIn('UA', untrusted_str)
        self.assertIn('George Bush Intercontinental Airport (IAH)', combined,
                      "the office name map must be passed in trusted or untrusted")
        self.assertIn('United Airlines (UA)', combined)


# ---------------------------------------------------------------------------
# 6. Per-row checks (a-g): force fallback for THAT row only.
# ---------------------------------------------------------------------------

class PerRowFallbackChecksTests(_TimelineAITestCase):
    """Each test breaks exactly ONE eligible row's AI text in one specific
    way; the OTHER rows must keep their (distinct) good AI text unchanged."""

    def _assert_others_untouched(self, tl, skip):
        if 0 not in skip:
            self.assertEqual(self._row(tl, '11:54')['text'], GOOD_CALL_TEXT)
        if 1 not in skip:
            # good filing text has balanced ** -> rendered <strong>, proving
            # it's still the AI's line and not the (markdown-free) fallback.
            self.assertIn('<strong>', self._row(tl, '12:07')['activity'])
        if 2 not in skip:
            self.assertEqual(self._row(tl, '12:15')['text'], GOOD_EMAIL_TEXT)
        if 3 not in skip:
            self.assertEqual(self._row(tl, '13:05')['text'], GOOD_REPLY_TEXT)

    def test_a_fabricated_number_falls_back(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        bad = ('Lost-item information was submitted through IAH, TSA, and UA channels '
              'within 48 hours')  # "48" is nowhere in the source/context/map
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({1: bad}))
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '12:07')['text'], FALLBACK_FILING)
        self.assertNotIn('<strong>', self._row(tl, '12:07')['activity'])
        self._assert_others_untouched(tl, skip={1})

    def test_b_wrong_call_duration_falls_back(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        bad = ('We called the customer (3m 10s) to confirm the lost-item details and '
              'discuss the investigation process')
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({0: bad}))
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '11:54')['text'], FALLBACK_CALL)
        self._assert_others_untouched(tl, skip={0})

    def test_c_unsupported_voicemail_claim_falls_back(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        # Duration is correct, but the call in this fixture connected (it has
        # a recording summary + recorded acceptance) — "voicemail" is unsupported.
        bad = 'We called the customer (2m 50s) but reached voicemail'
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({0: bad}))
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '11:54')['text'], FALLBACK_CALL)
        self._assert_others_untouched(tl, skip={0})

    def test_d_filing_omitting_an_office_code_falls_back(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        bad = 'Lost-item information was submitted through IAH and TSA channels'  # no UA
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({1: bad}))
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '12:07')['text'], FALLBACK_FILING)
        self._assert_others_untouched(tl, skip={1})

    def test_e_staff_name_falls_back(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        bad = 'Agent One emailed the customer with an update on their case'
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({2: bad}))
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '12:15')['text'], FALLBACK_EMAIL)
        self._assert_others_untouched(tl, skip={2})

    def test_f_too_long_falls_back(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        bad = ('The customer replied to us with a long follow-up message asking many '
              'detailed questions about the case. ' * 4)
        self.assertGreater(len(bad), 300)
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({3: bad}))
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '13:05')['text'], FALLBACK_REPLY)
        self._assert_others_untouched(tl, skip={3})

    def test_g_empty_text_falls_back_for_every_row_kind(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        bundle, _ = self._build_bundle(
            dispute, comments, _fake_rows({0: '', 1: '', 2: '', 3: ''}))
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '11:54')['text'], FALLBACK_CALL)
        self.assertEqual(self._row(tl, '12:07')['text'], FALLBACK_FILING)
        self.assertEqual(self._row(tl, '12:15')['text'], FALLBACK_EMAIL)
        self.assertEqual(self._row(tl, '13:05')['text'], FALLBACK_REPLY)


# ---------------------------------------------------------------------------
# 7. Clean-ups that KEEP the AI text (not a fallback).
# ---------------------------------------------------------------------------

class AITextCleanupTests(_TimelineAITestCase):

    def test_em_and_en_dash_become_a_comma(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        em = ('We called the customer (2m 50s) — confirming the lost-item details and '
             'setting expectations')
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({0: em}))
        row = self._row(bundle['timeline'], '11:54')
        self.assertNotIn('—', row['text'])
        self.assertNotIn('–', row['text'])
        self.assertNotIn('—', row['activity'])
        self.assertNotEqual(row['text'], FALLBACK_CALL)  # AI text kept, not rejected
        self.assertIn('confirming the lost-item details', row['text'])
        self.assertIn(',', row['text'])

    def test_unbalanced_asterisks_are_stripped_not_bolded(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        bad_markdown = ('Lost-item information was submitted through **IAH, TSA, and UA '
                        'channels today')  # one lone '**' -> unbalanced
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({1: bad_markdown}))
        row = self._row(bundle['timeline'], '12:07')
        self.assertNotIn('**', row['text'])
        self.assertNotIn('**', row['activity'])
        self.assertNotIn('<strong>', row['activity'])
        self.assertNotEqual(row['text'], FALLBACK_FILING)  # AI text kept, not rejected
        self.assertIn('IAH', row['text'])
        self.assertIn('today', row['text'])

    def test_more_than_three_bold_spans_are_stripped_not_bolded(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        four_spans = ('Lost-item information was submitted through **IAH**, **TSA**, **UA**, '
                     'and **the airline** channels')
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({1: four_spans}))
        row = self._row(bundle['timeline'], '12:07')
        self.assertNotIn('**', row['text'])
        self.assertNotIn('**', row['activity'])
        self.assertNotIn('<strong>', row['activity'])
        self.assertIn('the airline', row['text'])

    def test_html_in_ai_text_is_escaped_in_activity_and_rendered_report(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        malicious = ('We sent the customer an update <script>alert(1)</script> about their '
                    'filed report')
        bundle, _ = self._build_bundle(dispute, comments, _fake_rows({2: malicious}))
        row = self._row(bundle['timeline'], '12:15')
        self.assertNotIn('<script>', row['activity'])
        self.assertNotEqual(row['text'], FALLBACK_EMAIL)  # kept, not rejected

        from django.template.loader import render_to_string
        html = render_to_string(ds.report_template_for(dispute), bundle)
        self.assertNotIn('<script>alert(1)</script>', html)


# ---------------------------------------------------------------------------
# 8. Robustness to a broken/malformed AI reply.
# ---------------------------------------------------------------------------

class TimelineRobustnessTests(_TimelineAITestCase):

    def _assert_fixed_rows_unchanged(self, tl):
        self.assertEqual(self._row(tl, '10:12')['text'], DET_CLAIM_SUBMITTED)
        self.assertEqual(self._row(tl, '10:48')['text'], DET_PAYPAL_FILED)
        self.assertEqual(self._row(tl, '11:56')['text'], DET_ACCEPTANCE)
        notif = self._row(tl, '09:29')
        self.assertIn('Sep 20', notif['when'])
        self.assertEqual(notif['text'], DET_NOTIFICATION)

    def test_ai_raising_falls_back_report_still_renders(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        bundle, _ = self._build_bundle(dispute, comments, RuntimeError('provider down'))
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '11:54')['text'], FALLBACK_CALL)
        self.assertEqual(self._row(tl, '12:07')['text'], FALLBACK_FILING)
        self.assertEqual(self._row(tl, '12:15')['text'], FALLBACK_EMAIL)
        self.assertEqual(self._row(tl, '13:05')['text'], FALLBACK_REPLY)
        self._assert_fixed_rows_unchanged(tl)

    def test_ai_returning_none_falls_back_report_still_renders(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        bundle, _ = self._build_bundle(dispute, comments, None)
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '11:54')['text'], FALLBACK_CALL)
        self.assertEqual(self._row(tl, '12:07')['text'], FALLBACK_FILING)
        self.assertEqual(self._row(tl, '12:15')['text'], FALLBACK_EMAIL)
        self.assertEqual(self._row(tl, '13:05')['text'], FALLBACK_REPLY)
        self._assert_fixed_rows_unchanged(tl)

    def test_duplicate_index_falls_back_for_that_row_only(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        fake = types.SimpleNamespace(rows=[
            types.SimpleNamespace(index=0, activity=GOOD_CALL_TEXT + ' (first)'),
            types.SimpleNamespace(index=0, activity=GOOD_CALL_TEXT + ' (second)'),
            types.SimpleNamespace(index=1, activity=GOOD_FILING_TEXT),
            types.SimpleNamespace(index=2, activity=GOOD_EMAIL_TEXT),
            types.SimpleNamespace(index=3, activity=GOOD_REPLY_TEXT),
        ])
        bundle, _ = self._build_bundle(dispute, comments, fake)
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '11:54')['text'], FALLBACK_CALL)
        self.assertIn('<strong>', self._row(tl, '12:07')['activity'])
        self.assertEqual(self._row(tl, '12:15')['text'], GOOD_EMAIL_TEXT)
        self.assertEqual(self._row(tl, '13:05')['text'], GOOD_REPLY_TEXT)

    def test_out_of_range_index_is_ignored(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        fake = types.SimpleNamespace(rows=[
            types.SimpleNamespace(index=0, activity=GOOD_CALL_TEXT),
            types.SimpleNamespace(index=1, activity=GOOD_FILING_TEXT),
            types.SimpleNamespace(index=2, activity=GOOD_EMAIL_TEXT),
            types.SimpleNamespace(index=3, activity=GOOD_REPLY_TEXT),
            types.SimpleNamespace(index=42, activity='This index has no matching row'),
        ])
        bundle, _ = self._build_bundle(dispute, comments, fake)
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '11:54')['text'], GOOD_CALL_TEXT)
        self.assertIn('<strong>', self._row(tl, '12:07')['activity'])
        self.assertEqual(self._row(tl, '12:15')['text'], GOOD_EMAIL_TEXT)
        self.assertEqual(self._row(tl, '13:05')['text'], GOOD_REPLY_TEXT)
        self.assertEqual(len(tl), 8)  # index 42 must not spawn a 9th row

    def test_omitted_indices_fall_back_only_for_the_missing_rows(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        fake = types.SimpleNamespace(rows=[
            types.SimpleNamespace(index=0, activity=GOOD_CALL_TEXT),
            types.SimpleNamespace(index=1, activity=GOOD_FILING_TEXT),
            # index 2 (email) and 3 (reply) omitted entirely.
        ])
        bundle, _ = self._build_bundle(dispute, comments, fake)
        tl = bundle['timeline']
        self.assertEqual(self._row(tl, '11:54')['text'], GOOD_CALL_TEXT)
        self.assertIn('<strong>', self._row(tl, '12:07')['activity'])
        self.assertEqual(self._row(tl, '12:15')['text'], FALLBACK_EMAIL)
        self.assertEqual(self._row(tl, '13:05')['text'], FALLBACK_REPLY)

    def test_flood_of_indices_never_touches_the_fixed_rows(self):
        dispute, claim, comments = self._make_case(FULL_COMMENTS)
        fake = types.SimpleNamespace(rows=[
            types.SimpleNamespace(index=i, activity=f'filler activity text number {i}')
            for i in range(21)
        ])
        bundle, _ = self._build_bundle(dispute, comments, fake)
        tl = bundle['timeline']
        self._assert_fixed_rows_unchanged(tl)
        # 4 eligible rows + 4 fixed rows — the flood (indices 4..20) must not
        # invent extra rows.
        self.assertEqual(len(tl), 8)
        # The AI can never move a fixed row's timestamp or the overall order.
        whens = [e['when'] for e in tl]
        self.assertTrue(whens[0].endswith('10:12'))
        self.assertTrue(whens[-1].endswith('09:29') and 'Sep 20' in whens[-1])


# ---------------------------------------------------------------------------
# 9. Substantive internal "update" notes.
# ---------------------------------------------------------------------------

class InternalUpdateNoteTimelineTests(_TimelineAITestCase):

    def test_substantive_update_note_row_appears_with_ai_text(self):
        dispute, claim, comments = self._make_case(MINIMAL_COMMENTS + [MCO_UPDATE_COMMENT])
        fake = types.SimpleNamespace(rows=[
            types.SimpleNamespace(index=0, activity=GOOD_CALL_TEXT),
            types.SimpleNamespace(index=1, activity=GOOD_FILING_TEXT),
            types.SimpleNamespace(index=2, activity=GOOD_MCO_TEXT),
        ])
        bundle, _ = self._build_bundle(dispute, comments, fake)
        row = self._row(bundle['timeline'], '14:00')
        self.assertEqual(row['text'], GOOD_MCO_TEXT)

    def test_substantive_update_note_produces_no_row_when_ai_returns_empty(self):
        dispute, claim, comments = self._make_case(MINIMAL_COMMENTS + [MCO_UPDATE_COMMENT])
        fake = types.SimpleNamespace(rows=[
            types.SimpleNamespace(index=0, activity=GOOD_CALL_TEXT),
            types.SimpleNamespace(index=1, activity=GOOD_FILING_TEXT),
            types.SimpleNamespace(index=2, activity=''),
        ])
        bundle, _ = self._build_bundle(dispute, comments, fake)
        tl = bundle['timeline']
        self.assertEqual([e for e in tl if '14:00' in e['when']], [],
                         "an empty AI answer for an internal-update row must suppress it, "
                         "not fall back to a generic sentence")

    def test_substantive_update_note_produces_no_row_when_ai_off(self):
        dispute, claim, comments = self._make_case(MINIMAL_COMMENTS + [MCO_UPDATE_COMMENT])
        bundle, mock_complete = self._build_bundle(
            dispute, comments, fake_timeline_result=_fake_rows({}), use_ai=False)
        mock_complete.assert_not_called()
        tl = bundle['timeline']
        self.assertEqual([e for e in tl if '14:00' in e['when']], [])

    def test_short_near_empty_update_note_is_never_a_row(self):
        dispute, claim, comments = self._make_case(MINIMAL_COMMENTS + [SHORT_UPDATE_COMMENT])
        # Even if the AI tried to say SOMETHING for every plausible slot, the
        # short note must never surface — it was never eligible to begin with.
        fake = types.SimpleNamespace(rows=[
            types.SimpleNamespace(index=i, activity=f'should never be used ({i})')
            for i in range(6)
        ])
        bundle, _ = self._build_bundle(dispute, comments, fake)
        tl = bundle['timeline']
        self.assertEqual([e for e in tl if '14:30' in e['when']], [])


# ---------------------------------------------------------------------------
# 10. Provenance — generated_by honestly reflects the new timeline writer too.
# ---------------------------------------------------------------------------

class TimelineProvenanceTests(_TimelineAITestCase):
    """Mirrors ImageOnlyProvenanceTests (test_dispute_editing_safety_round2.py):
    when the section narrator and vision BOTH return None (nothing to place),
    but the new timeline writer alone produced usable rows, the document must
    still be labelled AI, not MANUAL."""

    def test_generated_by_ai_when_only_the_timeline_writer_succeeds(self):
        dispute, claim, comments = self._make_case(MINIMAL_COMMENTS)
        fake = types.SimpleNamespace(rows=[
            types.SimpleNamespace(index=0, activity=GOOD_CALL_TEXT),
            types.SimpleNamespace(index=1, activity=GOOD_FILING_TEXT),
        ])

        def _side_effect(**kwargs):
            if kwargs.get('call_site') == 'dispute_timeline':
                return fake
            raise Exception(f"unexpected call_site {kwargs.get('call_site')!r}")

        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {'id': dispute.zd_ticket_id},
                                       'comments': comments}), \
             patch.object(ds, '_attachment_data_uri', return_value='data:image/png;base64,AAAA'), \
             patch.object(ds, '_render_to_pdf', return_value=b'%PDF-1.4 fake'), \
             patch.object(ds, '_narrate_evidence', return_value=None) as narrate_evidence, \
             patch.object(ds, '_narrate_image_evidence', return_value=None) as narrate_vision, \
             patch('apps.payments.document_service.AIClient.complete',
                  side_effect=_side_effect):
            doc = ds.generate_evidence_report(dispute.id)

        self.assertIsNotNone(doc, "report generation must succeed")
        self.assertEqual(
            doc.generated_by, DisputeDocument.GENERATED_BY_AI,
            "generated_by must read AI when the timeline writer alone placed usable rows, "
            "even though the section narrator and vision both returned None")
