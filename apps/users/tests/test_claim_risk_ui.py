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
