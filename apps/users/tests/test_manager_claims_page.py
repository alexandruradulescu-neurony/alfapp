"""Render tests for the modernized manager Claims Overview page (2026-06-12)."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings

User = get_user_model()


class ManagerClaimsPageTests(TestCase):
    URL = '/manager/claims/'

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.zd_subdomain = 'testco'
        ss.save()
        self.manager = User.objects.create_user(
            username='page_manager', password='x', role='MANAGER')
        self.web = Client()
        self.web.force_login(self.manager)

        now = timezone.now()
        self.overdue = Claim.objects.create(
            client_email='ana@example.com', client_name='Ana Popescu',
            zd_ticket_id='93001', alf_claim_id='ALF9300001',
            status='Claim submitted', status_category='open',
            deadline_at=now - timedelta(days=3),
            status_changed_at=now - timedelta(days=20),
        )
        self.solved = Claim.objects.create(
            client_email='done@example.com', client_name='Done Client',
            zd_ticket_id='93002', alf_claim_id='ALF9300002',
            status='Closed - Refunded', status_category='solved',
        )
        EmailLog.objects.create(
            claim=self.overdue, subject='x', body='x',
            action_required=True, auto_resolved=False)

    def test_default_view_shows_urgency_and_hides_solved(self):
        resp = self.web.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # Client name leads; email is secondary
        self.assertIn('Ana Popescu', html)
        # Urgency surfaced: overdue label + stuck-in-status marker
        self.assertIn('overdue', html)
        self.assertIn('for 20d', html)
        # Attention badge from the action-required email
        self.assertIn('envelope-exclamation', html)
        # Deep link to the Zendesk ticket
        self.assertIn('https://testco.zendesk.com/agent/tickets/93001', html)
        # Solved claims hidden by default ('Active' family filter)
        self.assertNotIn('Done Client', html)
        # Bulk delete controls present
        self.assertIn('select-all', html)
        # Dropped columns are gone
        self.assertNotIn('<th>Evidence</th>', html)
        self.assertNotIn('<th>Created</th>', html)

    def test_family_all_shows_solved_too(self):
        resp = self.web.get(self.URL + '?family=all')
        html = resp.content.decode()
        self.assertIn('Done Client', html)
        self.assertIn('Ana Popescu', html)

    def test_headline_stats_count_whole_book(self):
        # conftest seeds claims session-wide → compare against the DB, not
        # absolute numbers. Filtering to 'solved' must not change the stats.
        resp = self.web.get(self.URL + '?family=solved')
        stats = resp.context['stats']
        self.assertEqual(stats['total'], Claim.objects.count())
        self.assertEqual(stats['active'],
                         Claim.objects.exclude(status_category='solved').count())
        self.assertGreaterEqual(stats['overdue'], 1)
        self.assertGreaterEqual(stats['attention'], 1)

    def test_search_finds_by_client_name(self):
        resp = self.web.get(self.URL + '?family=all&search=Popescu')
        html = resp.content.decode()
        self.assertIn('Ana Popescu', html)
        self.assertNotIn('Done Client', html)

    def test_raw_deadline_date_without_computed_moment_still_shows(self):
        # Claims from before the status mirror carry only deadline_date;
        # the computed deadline_at is null. The page must still show and
        # urgency-label them (this was 'deadline shows nothing' in prod).
        Claim.objects.create(
            client_email='old@example.com', client_name='Old Style',
            zd_ticket_id='93003', alf_claim_id='ALF9300003',
            status='Claim submitted', status_category='open',
            deadline_date=(timezone.now() + timedelta(days=3)).date(),
        )
        resp = self.web.get(self.URL)
        html = resp.content.decode()
        self.assertIn('Old Style', html)
        self.assertIn('d left', html)
