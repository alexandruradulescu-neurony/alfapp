"""Render tests for the redesigned manager Claims list — action-first tabs,
single Attention column, clickable rows (2026-06-19). The tab/lens logic itself
is covered in test_manager_claims_tabs.py."""

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
        self.manager = User.objects.create_user(username='page_manager', password='x')
        self.web = Client()
        self.web.force_login(self.manager)

        now = timezone.now()
        # A "problem": an institution email awaiting a human reply.
        self.problem = Claim.objects.create(
            client_email='ana@example.com', client_name='Ana Popescu',
            zd_ticket_id='93001', alf_claim_id='ALF9300001',
            status='Claim submitted', status_category='open',
            status_changed_at=now - timedelta(days=20),
        )
        EmailLog.objects.create(
            claim=self.problem, subject='x', body='x',
            action_required=True, auto_resolved=False)
        self.solved = Claim.objects.create(
            client_email='done@example.com', client_name='Done Client',
            zd_ticket_id='93002', alf_claim_id='ALF9300002',
            status='Closed - Refunded', status_category='solved',
        )

    def test_default_problems_tab_shows_attention_and_hides_solved(self):
        resp = self.web.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('Ana Popescu', html)               # client name leads
        self.assertIn('envelope-exclamation', html)       # attention: email awaiting reply
        self.assertIn('https://testco.zendesk.com/agent/tickets/93001', html)
        self.assertNotIn('Done Client', html)             # solved → not a problem
        self.assertIn('select-all', html)                 # bulk-select kept

    def test_tab_all_shows_solved_too(self):
        html = self.web.get(self.URL + '?tab=all').content.decode()
        self.assertIn('Done Client', html)
        self.assertIn('Ana Popescu', html)

    def test_search_within_tab(self):
        html = self.web.get(self.URL + '?tab=all&search=Popescu').content.decode()
        self.assertIn('Ana Popescu', html)
        self.assertNotIn('Done Client', html)

    def test_dropped_elements_gone(self):
        html = self.web.get(self.URL + '?tab=all').content.decode()
        self.assertNotIn('<th>Deadline</th>', html)       # deadline column removed
        self.assertNotIn('bi-file-earmark-pdf', html)     # PDF icon removed
        self.assertNotIn('All claims ever', html)         # stat cards removed
