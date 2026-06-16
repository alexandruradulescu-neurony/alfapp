from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.claims.models import Claim
from django.contrib.auth import get_user_model

User = get_user_model()


EXTRACTED = {
    'client_email': 'new@example.com', 'client_name': 'Ana Pop',
    'flight_details': 'RO301 2026-06-01 OTP-CDG', 'object_description': 'Black wallet',
    'phone': '+40712345678', 'alternate_email': '', 'claim_number': 'ALF1234567',
    'billing_address': '', 'shipping_address': 'Str. Lunga 1, Brasov',
    'incident_details': '', 'lost_location': 'Gate 12', 'deadline_date': '2026-07-01',
    'deadline_time': '17:00', 'deadline_timezone': 'CET', 'price_paid': '49.00',
    'payment_method': 'PayPal', 'payment_status': 'paid', 'woocommerce_id': '991',
    'tracking_info': '',
}


class RefreshFromZendeskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='refresh_agent1', password='x')
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)
        self.claim = Claim.objects.create(
            client_email='old@example.com', zd_ticket_id='70001',
            object_description='Existing description kept',
            flight_details='OLD FLIGHT')
        self.url = f'/api/claims/{self.claim.id}/update-from-zendesk/'

    def _run(self, refresh_ok=True):
        with patch('apps.claims.views.fetch_zendesk_ticket',
                   return_value={'subject': 'ALF1234567', 'description': 'd',
                                 'custom_fields': [], 'created_at': 'x'}), \
             patch('apps.claims.views.fetch_zendesk_comments', return_value=[]), \
             patch('apps.claims.views.analyze_zendesk_ticket_for_claim',
                   return_value=dict(EXTRACTED)), \
             patch('apps.claims.views.refresh_claim_summary',
                   return_value=refresh_ok):
            return self.client_api.post(self.url)

    def test_structured_fields_overwrite_and_fill_only_respected(self):
        response = self._run()
        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.flight_details, 'RO301 2026-06-01 OTP-CDG')
        self.assertEqual(self.claim.object_description, 'Existing description kept')
        self.assertEqual(self.claim.shipping_address, 'Str. Lunga 1, Brasov')
        self.assertIsNotNone(self.claim.deadline_at)
        self.assertEqual(str(self.claim.price_paid), '49.00')

    def test_timeline_entry_written(self):
        self._run()
        entry = self.claim.updates.first()
        self.assertEqual(entry.update_type, 'INFO_UPDATED')
        self.assertIn('flight_details', entry.changes_summary)

    def test_status_is_never_touched(self):
        before = self.claim.status
        self._run()
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, before)

    def test_structured_fields_overwrite_and_fill_only_respected_email(self):
        self._run()
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.client_email, 'new@example.com')

    def test_summary_failure_still_succeeds(self):
        response = self._run(refresh_ok=False)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIs(data['summary_refreshed'], False)
        from apps.claims.models import ClaimUpdateTimeline
        entry = ClaimUpdateTimeline.objects.filter(claim=self.claim).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.llm_summary, '')

    def test_second_run_reports_no_changes(self):
        self._run()
        response2 = self._run()
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response2.json()['updated_fields'], [])
