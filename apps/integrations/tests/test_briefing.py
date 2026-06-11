from unittest.mock import patch

from django.test import TestCase

from apps.claims.models import Claim


def _fake_briefing(summary='AI summary of the case.'):
    from apps.ai.schemas import BriefingSummary
    return BriefingSummary(summary=summary, next_steps=[])


class NormalizeFetchedCommentsTests(TestCase):
    def test_author_dict_and_body_are_flattened(self):
        from apps.integrations.briefing import normalize_fetched_comments
        raw = [{'author': {'id': 1, 'name': 'TSA Office', 'email': 't@x.gov'},
                'body': 'No match found yet.', 'public': False,
                'created_at': '2026-06-01T10:00:00Z'}]
        result = normalize_fetched_comments(raw)
        self.assertEqual(result, [{'author': 'TSA Office',
                                   'created_at': '2026-06-01T10:00:00Z',
                                   'public': False,
                                   'text': 'No match found yet.'}])

    def test_non_dict_entries_are_skipped(self):
        from apps.integrations.briefing import normalize_fetched_comments
        self.assertEqual(normalize_fetched_comments(['plain', None]), [])


class GenerateClaimSummaryTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(
            client_email='sum@example.com', client_name='Ana Pop',
            zd_ticket_id='777', object_description='Black wallet')
        self.ticket_data = {'subject': 'ALF1234567', 'description': 'Lost wallet',
                            'created_at': '2026-06-01T09:00:00Z', 'comments': []}

    @patch('apps.integrations.briefing.AIClient.complete')
    def test_returns_summary_and_passes_client_name_as_known_pii(self, mock_complete):
        from apps.integrations.briefing import generate_claim_summary
        mock_complete.return_value = _fake_briefing('Case is searching.')
        result = generate_claim_summary(self.claim, self.ticket_data)
        self.assertEqual(result, 'Case is searching.')
        kwargs = mock_complete.call_args.kwargs
        self.assertIn('Ana Pop', kwargs['known_pii']['names'])
        self.assertEqual(kwargs['call_site'], 'claim_summary')

    @patch('apps.integrations.briefing.AIClient.complete', side_effect=RuntimeError('AI down'))
    def test_ai_failure_returns_none(self, mock_complete):
        from apps.integrations.briefing import generate_claim_summary
        self.assertIsNone(generate_claim_summary(self.claim, self.ticket_data))


class RefreshClaimSummaryTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(
            client_email='ref@example.com', zd_ticket_id='778',
            ai_summary='old text')
        self.ticket_data = {'subject': 's', 'description': 'd', 'comments': []}

    @patch('apps.integrations.briefing.AIClient.complete')
    def test_success_stores_summary_and_timestamp(self, mock_complete):
        from apps.integrations.briefing import refresh_claim_summary
        mock_complete.return_value = _fake_briefing('Fresh summary.')
        self.assertTrue(refresh_claim_summary(self.claim, self.ticket_data))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.ai_summary, 'Fresh summary.')
        self.assertIsNotNone(self.claim.ai_summary_updated_at)

    @patch('apps.integrations.briefing.AIClient.complete', side_effect=RuntimeError('AI down'))
    def test_failure_keeps_old_summary(self, mock_complete):
        from apps.integrations.briefing import refresh_claim_summary
        self.assertFalse(refresh_claim_summary(self.claim, self.ticket_data))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.ai_summary, 'old text')
        self.assertIsNone(self.claim.ai_summary_updated_at)
