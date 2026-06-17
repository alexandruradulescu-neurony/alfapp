from unittest.mock import patch

from django.test import TestCase

from apps.claims.models import Claim
from apps.integrations.briefing import keyword_risk_reasons, merge_risk
from apps.integrations.views import webhooks


class KeywordBoosterTests(TestCase):
    def test_scam_flags_hostile(self):
        self.assertIn('hostile_language', keyword_risk_reasons('you people are a SCAM'))

    def test_chargeback_flags_dispute(self):
        self.assertIn('dispute_risk', keyword_risk_reasons('I will file a charge back'))

    def test_non_refundable_fee_is_not_flagged(self):
        self.assertEqual(keyword_risk_reasons('Client agreed to the non-refundable $76 fee'), set())

    def test_routine_dispute_word_not_flagged(self):
        self.assertEqual(keyword_risk_reasons('opened a PayPal dispute case earlier'), set())


class MergeRiskTests(TestCase):
    def test_ai_hard_reason_is_at_risk(self):
        level, reasons, _ = merge_risk(ai_level='at_risk', ai_reasons=['refund_demanded'],
                                       ai_note='wants money back', thread_text='')
        self.assertEqual(level, 'at_risk')
        self.assertIn('refund_demanded', reasons)

    def test_keyword_only_hard_reason_caps_at_watch(self):
        level, reasons, _ = merge_risk(ai_level='none', ai_reasons=[],
                                       ai_note='', thread_text='this is NOT a scam, just asking')
        self.assertEqual(level, 'watch')
        self.assertIn('hostile_language', reasons)

    def test_soft_sentiment_is_watch(self):
        level, _, _ = merge_risk(ai_level='watch', ai_reasons=['negative_sentiment'],
                                 ai_note='frustrated', thread_text='')
        self.assertEqual(level, 'watch')

    def test_clean_is_none(self):
        level, reasons, _ = merge_risk(ai_level='none', ai_reasons=[], ai_note='', thread_text='all good')
        self.assertEqual(level, 'none')
        self.assertEqual(reasons, [])


def _solved_claim():
    return Claim.objects.create(client_email='r@example.com', zd_ticket_id='90100',
                                alf_claim_id='ALF9010000', status='Solved', status_category='solved')


class StatusRegressionTests(TestCase):
    @patch('apps.integrations.views.webhooks.refresh_claim_summary', return_value=True)
    @patch('apps.integrations.views.webhooks.fetch_zendesk_ticket', return_value={})
    @patch('apps.integrations.views.webhooks.fetch_zendesk_comments', return_value=[])
    @patch('apps.integrations.views.webhooks.resolve_custom_status',
           return_value={'name': 'Investigation initiated', 'category': 'open'})
    def test_solved_to_open_flags_regression(self, *_mocks):
        c = _solved_claim()
        webhooks.mirror_status_change(c, custom_status_id='123')
        c.refresh_from_db()
        self.assertIn('status_regression', c.risk_reasons)
        self.assertEqual(c.risk_level, 'at_risk')
        self.assertTrue(c.risk_active)

    @patch('apps.integrations.views.webhooks.refresh_claim_summary', return_value=True)
    @patch('apps.integrations.views.webhooks.fetch_zendesk_ticket', return_value={})
    @patch('apps.integrations.views.webhooks.fetch_zendesk_comments', return_value=[])
    @patch('apps.integrations.views.webhooks.resolve_custom_status',
           return_value={'name': 'Solved', 'category': 'solved'})
    def test_forward_to_solved_does_not_flag(self, *_mocks):
        c = Claim.objects.create(client_email='f@example.com', zd_ticket_id='90101',
                                 alf_claim_id='ALF9010100', status='Claim submitted', status_category='open')
        webhooks.mirror_status_change(c, custom_status_id='456')
        c.refresh_from_db()
        self.assertNotIn('status_regression', c.risk_reasons)
