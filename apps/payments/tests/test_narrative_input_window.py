"""Red-phase spec — the PayPal-note AI's input window (item 8): pin down
_narrative_untrusted's NEW record cap and per-record truncation so the AI sees
enough of the case to write an accurate note, without silently dropping the
most recent evidence.

Deliberately unimplemented — these tests must fail until
document_service._narrative_untrusted's defaults change from
(max_comments=8, per_comment_chars=400) to (40, 700), with a selection rule
that, once there are more records than the cap, keeps the EARLIEST 8 plus the
MOST RECENT (cap - 8) records (chronological order preserved) instead of just
truncating to the first N.
"""

from django.test import TestCase

from apps.payments import document_service as ds


def _bundle(n):
    """The minimal structure _narrative_untrusted needs: bundle['panels'], an
    ORDERED list of dicts with 'body'/'public' — mirrors what
    build_dispute_narrative_notes passes it (bundle['panels'] from
    build_dispute_evidence_bundle, chronologically ordered comment panels)."""
    return {'panels': [{'body': f'RECORD-{i:02d} case note text.', 'public': bool(i % 2)}
                        for i in range(1, n + 1)]}


class RecordCapTests(TestCase):
    def test_default_cap_is_40_not_8(self):
        # 30 records, all under the new 40 cap — none should be dropped. Under
        # the OLD default (max_comments=8) only RECORD-01..08 would survive.
        bundle = _bundle(30)
        out = ds._narrative_untrusted(bundle)
        kept = out.get('zendesk_comment', [])
        rendered = '\n'.join(kept)
        self.assertEqual(
            len(kept), 30,
            f"expected all 30 records kept under the new 40 cap; got {len(kept)}")
        for i in range(1, 31):
            self.assertIn(f'RECORD-{i:02d}', rendered,
                         f"RECORD-{i:02d} missing — still capping at 8?")

    def test_over_cap_keeps_earliest_8_and_most_recent_32_in_order(self):
        # 50 records, over the new 40 cap: keep the first 8 (RECORD-01..08) and
        # the most recent 32 (RECORD-19..50); drop the middle 10 (RECORD-09..18).
        bundle = _bundle(50)
        out = ds._narrative_untrusted(bundle)
        kept = out.get('zendesk_comment', [])
        rendered = '\n'.join(kept)
        self.assertEqual(
            len(kept), 40,
            f"50 records over a cap of 40 must total exactly 40 kept; got {len(kept)}")
        for i in range(1, 9):
            self.assertIn(f'RECORD-{i:02d}', rendered,
                         f"RECORD-{i:02d} is one of the first 8 and must be kept")
        for i in range(19, 51):
            self.assertIn(f'RECORD-{i:02d}', rendered,
                         f"RECORD-{i:02d} is one of the most recent 32 and must be kept")
        for i in range(9, 19):
            self.assertNotIn(f'RECORD-{i:02d}', rendered,
                             f"RECORD-{i:02d} is a dropped middle record and must be excluded")
        # Chronological order preserved: the first-8 block, then the recent
        # run — never reshuffled by the selection.
        kept_indexes = list(range(1, 9)) + list(range(19, 51))
        positions = [rendered.index(f'RECORD-{i:02d}') for i in kept_indexes]
        self.assertEqual(positions, sorted(positions),
                         "kept records must stay in chronological order")


class PerRecordTruncationTests(TestCase):
    def test_700_char_record_is_kept_whole(self):
        body700 = 'A' * 699 + 'Y'   # length 700; the final char must survive
        bundle = {'panels': [{'body': body700, 'public': True}]}
        out = ds._narrative_untrusted(bundle)
        rendered = out['zendesk_comment'][0]
        self.assertIn(body700, rendered,
                     "a 700-char record must not be truncated under the new 700-char cap")

    def test_800_char_record_is_cut_and_its_800th_char_is_absent(self):
        body800 = 'C' * 799 + 'Z'   # length 800; 'Z' marks the 800th character
        bundle = {'panels': [{'body': body800, 'public': True}]}
        out = ds._narrative_untrusted(bundle)
        rendered = out['zendesk_comment'][0]
        self.assertIn('C' * 700, rendered, "the first 700 characters must survive whole")
        self.assertNotIn('Z', rendered, "the 800th character must be cut off at 700")
