from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.claims.models import Claim

User = get_user_model()


class ClaimBodyFragmentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='body_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='b@example.com', client_name='Bo Li',
            zd_ticket_id='95001', alf_claim_id='ALF95001',
            price_paid=Decimal('50.00'))

    def test_body_route_returns_fragment_not_full_page(self):
        resp = self.web.get(reverse('agent_claim_detail_body', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('id="claim-body"', html)
        self.assertNotIn('<html', html)  # fragment, not the full base shell

    def test_full_page_still_renders_and_contains_body(self):
        resp = self.web.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('<html', html)
        self.assertIn('id="claim-body"', html)


class FormActionHtmxTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='act_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='a@example.com', client_name='Ada Min',
            zd_ticket_id='95002', alf_claim_id='ALF95002',
            price_paid=Decimal('50.00'))

    def test_acknowledge_risk_htmx_returns_body_fragment(self):
        resp = self.web.post(
            reverse('claim_acknowledge_risk', args=[self.claim.id]),
            HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('id="claim-body"', html)
        self.assertNotIn('<html', html)

    def test_acknowledge_risk_non_htmx_still_redirects(self):
        resp = self.web.post(reverse('claim_acknowledge_risk', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/agent/claims/{self.claim.id}/', resp['Location'])
