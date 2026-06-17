from django.contrib.auth import get_user_model
from django.test import TestCase
from apps.claims.models import Claim

User = get_user_model()


def _claim():
    return Claim.objects.create(client_email='c@example.com', zd_ticket_id='90001',
                                alf_claim_id='ALF9000001')


class RegisterRiskTests(TestCase):
    def test_first_signal_sets_level_reasons_detail_and_timestamps(self):
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='Client demanded refund')
        c.refresh_from_db()
        self.assertEqual(c.risk_level, 'at_risk')
        self.assertEqual(c.risk_reasons, ['refund_demanded'])
        self.assertEqual(c.risk_detail, 'Client demanded refund')
        self.assertIsNotNone(c.risk_first_flagged_at)
        self.assertIsNotNone(c.risk_last_signal_at)

    def test_clean_pass_never_downgrades(self):
        c = _claim()
        c.register_risk(reasons=['hostile_language'], level='at_risk', detail='scam')
        c.register_risk(reasons=[], level='none', detail='')  # later cheerful read
        c.refresh_from_db()
        self.assertEqual(c.risk_level, 'at_risk')
        self.assertEqual(c.risk_reasons, ['hostile_language'])

    def test_reasons_union_and_severity_only_rises(self):
        c = _claim()
        c.register_risk(reasons=['negative_sentiment'], level='watch', detail='unhappy')
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='wants money back')
        c.refresh_from_db()
        self.assertEqual(set(c.risk_reasons), {'negative_sentiment', 'refund_demanded'})
        self.assertEqual(c.risk_level, 'at_risk')

    def test_first_flagged_at_is_stable_across_signals(self):
        c = _claim()
        c.register_risk(reasons=['negative_sentiment'], level='watch', detail='x')
        first = Claim.objects.get(pk=c.pk).risk_first_flagged_at
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='y')
        self.assertEqual(Claim.objects.get(pk=c.pk).risk_first_flagged_at, first)


class AcknowledgeRiskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr', password='x')

    def test_acknowledge_clears_active_keeps_history(self):
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        c.acknowledge_risk(self.user)
        c.refresh_from_db()
        self.assertFalse(c.risk_active)
        self.assertEqual(c.risk_level, 'at_risk')
        self.assertEqual(c.risk_reasons, ['refund_demanded'])
        self.assertEqual(c.risk_acknowledged_by, self.user)

    def test_same_signal_after_ack_does_not_reraise(self):
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        c.acknowledge_risk(self.user)
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d again')
        c.refresh_from_db()
        self.assertFalse(c.risk_active)

    def test_new_reason_after_ack_reraises(self):
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        c.acknowledge_risk(self.user)
        c.register_risk(reasons=['status_regression'], level='at_risk', detail='reopened')
        c.refresh_from_db()
        self.assertTrue(c.risk_active)
        self.assertIsNone(c.risk_acknowledged_at)
