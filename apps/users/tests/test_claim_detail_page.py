"""Render tests for the modernized claim detail page (2026-06-12)."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from apps.claims.models import Claim

User = get_user_model()

FLIGHT_DATA = {
    'number': 'A3 1234',
    'airline': 'Aegean Airlines',
    'status': 'Arrived',
    'verdict': {'level': 'check', 'label': '⚠️ Flight found — details need a check'},
    'legs': [{
        'from_iata': 'DEN', 'from_city': 'Denver',
        'to_iata': 'ATH', 'to_city': 'Athens',
        'scheduled_departure_local': '2026-06-04 10:45',
        'scheduled_arrival_local': '2026-06-05 07:10',
        'from_terminal': 'B', 'from_gate': '22',
        'to_terminal': '2', 'to_gate': '', 'to_baggage_belt': '7',
    }],
}


class ClaimDetailPageTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username='detail_manager', password='x')
        self.web = Client()
        self.web.force_login(self.manager)
        self.claim = Claim.objects.create(
            client_email='alex@example.com', client_name='Alex Radu',
            phone='+40 721 111 222', shipping_address='Str. Lunga 1, Brasov',
            email_alias='case-99@mailapptoday.com',
            zd_ticket_id='94001', alf_claim_id='ALF9400001',
            status='Pending', status_category='pending',
            object_description='AirPods case', lost_location='Check-in area',
            deadline_at=timezone.now() + timedelta(days=2),
            flight_data=FLIGHT_DATA,
            flight_data_updated_at=timezone.now(),
            flight_details='Flight: 1234 | Airline: Aegean Airlines - A3',
        )
        self.url = f'/agent/claims/{self.claim.id}/'

    def test_client_case_and_flight_cards_render(self):
        resp = self.web.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # Client card: name leads, phone, shipping address, case alias
        self.assertIn('Alex Radu', html)
        self.assertIn('+40 721 111 222', html)
        self.assertIn('Str. Lunga 1, Brasov', html)
        self.assertIn('case-99@mailapptoday.com', html)
        # Case card: object, location, deadline with urgency label
        self.assertIn('AirPods case', html)
        self.assertIn('Check-in area', html)
        self.assertIn('d left', html)  # 1d/2d depending on time of day
        # Flight card: verdict chip, route, facilities, freshness
        self.assertIn('details need a check', html)
        self.assertIn('DEN', html)
        self.assertIn('Athens', html)
        self.assertIn('Terminal B', html)
        self.assertIn('Belt 7', html)
        self.assertIn('via AeroDataBox', html)

    def test_unverified_flight_falls_back_to_raw_details(self):
        self.claim.flight_data = {}
        self.claim.save(update_fields=['flight_data'])
        resp = self.web.get(self.url)
        html = resp.content.decode()
        self.assertIn('Aegean Airlines - A3', html)
        self.assertIn('Not verified yet', html)

    def test_email_log_separates_open_from_handled(self):
        from apps.communications.models import EmailLog
        EmailLog.objects.create(
            claim=self.claim, subject='Needs a reply', body='please respond',
            category='OBJECT_FOUND', action_required=True, auto_resolved=False)
        EmailLog.objects.create(
            claim=self.claim, subject='Auto handled', body='not found',
            category='OBJECT_NOT_FOUND', action_required=False, auto_resolved=True)
        resp = self.web.get(self.url)
        html = resp.content.decode()
        # Open email is shown with its resolve control; handled section exists
        self.assertIn('Needs a reply', html)
        self.assertIn('Mark resolved', html)
        self.assertIn('need action', html)
        self.assertIn('Handled (1)', html)
        # The handled (auto-resolved) one is present but tucked in the section
        self.assertIn('Auto handled', html)
