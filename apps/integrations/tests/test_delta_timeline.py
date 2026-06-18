from unittest.mock import patch
from django.test import TestCase
from apps.claims.models import Claim
from apps.integrations import briefing
from apps.ai.schemas import BriefingSummary


def _claim(**kw):
    base = dict(client_email='c@example.com', zd_ticket_id='95001', alf_claim_id='ALF9500001')
    base.update(kw)
    return Claim.objects.create(**base)


def _fake_result(summary='Full current-state summary.', delta='Item located at BOS.',
                 risk_level='none', risk_reasons=None, risk_note=''):
    return BriefingSummary(summary=summary, delta=delta, risk_level=risk_level,
                           risk_reasons=risk_reasons or [], risk_note=risk_note)


class RefreshReturnsDeltaTests(TestCase):
    def test_returns_delta_and_stores_full_snapshot(self):
        c = _claim()
        with patch.object(briefing, 'generate_claim_summary', return_value=_fake_result()):
            delta = briefing.refresh_claim_summary(c, {'subject': '', 'comments': []},
                                                   previous_note='earlier note')
        self.assertEqual(delta, 'Item located at BOS.')
        c.refresh_from_db()
        self.assertEqual(c.ai_summary, 'Full current-state summary.')

    def test_empty_delta_coerced_to_no_new_information(self):
        c = _claim()
        with patch.object(briefing, 'generate_claim_summary', return_value=_fake_result(delta='')):
            delta = briefing.refresh_claim_summary(c, {'subject': '', 'comments': []})
        self.assertEqual(delta, 'No new information.')

    def test_ai_failure_returns_none_and_leaves_snapshot(self):
        c = _claim(ai_summary='OLD')
        with patch.object(briefing, 'generate_claim_summary', return_value=None):
            delta = briefing.refresh_claim_summary(c, {'subject': '', 'comments': []})
        self.assertIsNone(delta)
        c.refresh_from_db()
        self.assertEqual(c.ai_summary, 'OLD')

    def test_previous_note_is_passed_to_generation(self):
        c = _claim()
        with patch.object(briefing, 'generate_claim_summary', return_value=_fake_result()) as gen:
            briefing.refresh_claim_summary(c, {'subject': '', 'comments': []}, previous_note='PRIOR')
        _, kwargs = gen.call_args
        self.assertEqual(kwargs.get('previous_note'), 'PRIOR')


class PromptDeferToFactsTests(TestCase):
    def test_summary_prompt_tells_model_to_defer_to_facts(self):
        self.assertIn('defer to', briefing.SUMMARY_PROMPT.lower())

    def test_briefing_summary_schema_has_delta_default(self):
        bs = BriefingSummary(summary='x')
        self.assertEqual(bs.delta, '')
