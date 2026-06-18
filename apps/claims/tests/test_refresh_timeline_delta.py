from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from apps.claims.models import Claim, ClaimUpdateTimeline

User = get_user_model()


class ManualRefreshTimelineTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='u', password='x')
        self.client.force_login(self.user)
        self.claim = Claim.objects.create(client_email='c@example.com', zd_ticket_id='95200',
                                          alf_claim_id='ALF9520000')

    def _post(self):
        return self.client.post(f'/api/claims/{self.claim.id}/update-from-zendesk/')

    @patch('apps.claims.views.fetch_zendesk_ticket', return_value={'subject': 's'})
    @patch('apps.claims.views.fetch_zendesk_comments', return_value=[])
    @patch('apps.claims.views.analyze_zendesk_ticket_for_claim', return_value={})
    @patch('apps.claims.views.refresh_claim_summary', return_value='No new information.')
    def test_no_change_writes_no_timeline_row(self, *_m):
        with patch('apps.claims.views.refresh_claim_from_zendesk', return_value=[]):
            self._post()
        self.assertEqual(ClaimUpdateTimeline.objects.filter(claim=self.claim).count(), 0)

    @patch('apps.claims.views.fetch_zendesk_ticket', return_value={'subject': 's'})
    @patch('apps.claims.views.fetch_zendesk_comments', return_value=[])
    @patch('apps.claims.views.analyze_zendesk_ticket_for_claim', return_value={})
    @patch('apps.claims.views.refresh_claim_summary', return_value='No new information.')
    def test_changed_fields_write_deterministic_row(self, *_m):
        with patch('apps.claims.views.refresh_claim_from_zendesk', return_value=['phone', 'shipping_address']):
            self._post()
        entry = ClaimUpdateTimeline.objects.get(claim=self.claim)
        self.assertEqual(entry.update_type, 'INFO_UPDATED')
        self.assertIn('phone', entry.llm_summary.lower())
        self.assertIn('shipping', entry.llm_summary.lower())
        self.assertNotIn('No new information', entry.llm_summary)
