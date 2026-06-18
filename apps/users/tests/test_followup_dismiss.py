from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.claims.models import Claim
from apps.communications.models import ClientUpdate

User = get_user_model()


class DismissMissedMilestoneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='dismiss_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='d@example.com', zd_ticket_id='1', alf_claim_id='A')

    def test_dismiss_records_milestone_skipped(self):
        self.web.post(reverse('client_followup_dismiss', args=[self.claim.id, 'DAY_5']))
        cu = ClientUpdate.objects.get(claim=self.claim, milestone='DAY_5')
        self.assertEqual(cu.state, 'SKIPPED')
