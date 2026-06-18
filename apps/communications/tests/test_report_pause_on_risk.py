from django.test import TestCase
from apps.claims.models import Claim
from apps.communications.client_report import build_client_update_message
from apps.communications.client_updates import regenerate_initial_update


def _claim(**kw):
    base = dict(client_email='c@example.com', zd_ticket_id='97001', alf_claim_id='ALF9700001',
                client_name='Test Client', object_description='grey laptop')
    base.update(kw)
    return Claim.objects.create(**base)


class ReportHonestyOnRiskTests(TestCase):
    def test_at_risk_returns_review_notice_not_cheerful_template(self):
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='wants money back')
        msg = build_client_update_message(c, polish=False)
        self.assertIn('PAUSED', msg)
        self.assertIn('Refund demanded', msg)            # human-readable reason
        self.assertNotIn('formally reported', msg)        # NOT the cheerful boilerplate

    def test_not_at_risk_returns_normal_report(self):
        c = _claim()
        msg = build_client_update_message(c, polish=False)
        self.assertIn('formally reported', msg)           # normal content preserved

    def test_acknowledged_claim_is_not_paused(self):
        from django.contrib.auth import get_user_model
        u = get_user_model().objects.create_user(username='mgr', password='x')
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        c.acknowledge_risk(u)                              # risk_active now False
        msg = build_client_update_message(c, polish=False)
        self.assertIn('formally reported', msg)           # resumes normal once acknowledged

    def test_regenerate_writes_the_review_notice_when_at_risk(self):
        c = _claim()
        c.register_risk(reasons=['status_regression'], level='at_risk', detail='reopened')
        regenerate_initial_update(c)
        c.refresh_from_db()
        self.assertIn('PAUSED', c.client_report_draft)
