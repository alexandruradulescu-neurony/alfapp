from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from apps.claims.models import Claim

User = get_user_model()


class AcknowledgeRiskViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr', password='x')
        self.client.force_login(self.user)
        self.claim = Claim.objects.create(client_email='c@example.com', zd_ticket_id='90200',
                                          alf_claim_id='ALF9020000')
        self.claim.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')

    def test_post_acknowledges(self):
        resp = self.client.post(reverse('claim_acknowledge_risk', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 302)
        self.claim.refresh_from_db()
        self.assertFalse(self.claim.risk_active)
        self.assertEqual(self.claim.risk_acknowledged_by, self.user)

    def test_get_not_allowed(self):
        resp = self.client.get(reverse('claim_acknowledge_risk', args=[self.claim.id]))
        self.assertIn(resp.status_code, (405, 302))
        self.claim.refresh_from_db()
        self.assertTrue(self.claim.risk_active)

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.post(reverse('claim_acknowledge_risk', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 302)  # to login
        self.claim.refresh_from_db()
        self.assertTrue(self.claim.risk_active)


class RiskFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr2', password='x')
        self.client.force_login(self.user)
        self.flagged = Claim.objects.create(client_email='a@example.com', zd_ticket_id='90300',
                                             alf_claim_id='ALF9030000', client_name='Risky Rita')
        self.flagged.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        self.clean = Claim.objects.create(client_email='b@example.com', zd_ticket_id='90301',
                                          alf_claim_id='ALF9030100', client_name='Calm Carl')

    def test_risk_filter_shows_only_unacknowledged_flagged(self):
        resp = self.client.get(reverse('agent_claims') + '?risk=1')
        self.assertContains(resp, 'ALF9030000')
        self.assertNotContains(resp, 'ALF9030100')

    def test_unfiltered_shows_both(self):
        resp = self.client.get(reverse('agent_claims'))
        self.assertContains(resp, 'ALF9030000')
        self.assertContains(resp, 'ALF9030100')

    def test_acknowledged_claim_drops_out_of_risk_filter(self):
        self.flagged.acknowledge_risk(self.user)
        resp = self.client.get(reverse('agent_claims') + '?risk=1')
        self.assertNotContains(resp, 'ALF9030000')

    def test_badge_rendered_on_list(self):
        resp = self.client.get(reverse('agent_claims'))
        self.assertContains(resp, 'At risk')


class RiskBannerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr3', password='x')
        self.client.force_login(self.user)
        self.claim = Claim.objects.create(client_email='c@example.com', zd_ticket_id='90400',
                                          alf_claim_id='ALF9040000', client_name='Test')

    def test_banner_shown_when_at_risk(self):
        self.claim.register_risk(reasons=['refund_demanded'], level='at_risk',
                                 detail='Client demanded a refund')
        resp = self.client.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertContains(resp, 'Client demanded a refund')
        self.assertContains(resp, 'Acknowledge')

    def test_no_banner_when_clean(self):
        resp = self.client.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertNotContains(resp, 'Acknowledge')

    def test_no_banner_after_acknowledge(self):
        self.claim.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        self.claim.acknowledge_risk(self.user)
        resp = self.client.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertNotContains(resp, 'Acknowledge')

    def test_banner_shows_human_reason_labels(self):
        self.claim.register_risk(reasons=['status_regression'], level='at_risk', detail='Reopened')
        resp = self.client.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertContains(resp, 'Status reopened')
        self.assertNotContains(resp, 'status_regression')


class RiskBadgeSeverityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sev', password='x')
        self.client.force_login(self.user)

    def test_watch_level_shows_watch_pill_not_at_risk(self):
        c = Claim.objects.create(client_email='w@example.com', zd_ticket_id='90500',
                                 alf_claim_id='ALF9050000', client_name='Watchful')
        c.register_risk(reasons=['negative_sentiment'], level='watch', detail='mild')
        resp = self.client.get(reverse('agent_claims'))
        self.assertContains(resp, '⚠ Watch')
        self.assertNotContains(resp, '⚠ At risk')

    def test_at_risk_shows_at_risk_pill(self):
        c = Claim.objects.create(client_email='r2@example.com', zd_ticket_id='90501',
                                 alf_claim_id='ALF9050100', client_name='Risky')
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='refund')
        resp = self.client.get(reverse('agent_claims'))
        self.assertContains(resp, '⚠ At risk')
