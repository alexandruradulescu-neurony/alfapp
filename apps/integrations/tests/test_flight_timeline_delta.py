"""Flight-lookup timeline entries follow the delta convention: a concise,
deterministic one-liner built from the structured result, NOT the full AI
flight-match narration (which still goes to the Zendesk note)."""

from django.test import TestCase
from apps.claims.models import Claim
from apps.integrations.views import flight


def _claim():
    return Claim.objects.create(client_email='c@example.com', zd_ticket_id='96001',
                                alf_claim_id='ALF9600001')


class FlightTimelineDeltaTests(TestCase):
    def test_found_line_is_concise_and_deterministic(self):
        s = flight._flight_timeline_summary(
            {'number': 'RO301', 'date': '2026-06-01', 'found': True, 'verdict': 'strong'})
        self.assertIn('Flight lookup', s)
        self.assertIn('RO301', s)
        self.assertIn('found', s.lower())

    def test_not_found_with_candidates_line(self):
        s = flight._flight_timeline_summary(
            {'number': 'RO301', 'date': '2026-06-01', 'found': False, 'candidates': 3})
        self.assertIn('not found', s.lower())
        self.assertIn('3', s)

    def test_not_found_no_candidates_line(self):
        s = flight._flight_timeline_summary(
            {'number': 'RO301', 'date': '2026-06-01', 'found': False, 'candidates': 0})
        self.assertIn('not found', s.lower())

    def test_record_writes_deterministic_line_not_full_narration(self):
        c = _claim()
        flight._record_flight_timeline(
            c, {'number': 'RO301', 'date': '2026-06-01', 'found': True, 'verdict': 'strong'})
        entry = c.updates.first()
        self.assertEqual(entry.update_type, 'INFO_UPDATED')
        self.assertIn('Flight lookup', entry.llm_summary)
        self.assertIn('RO301', entry.llm_summary)
