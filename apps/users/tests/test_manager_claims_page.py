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
        self.assertIn('Institution emails awaiting a human reply', html)  # attention: email awaiting reply
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

    def test_numbered_pagination_when_many_pages(self):
        # 45 claims over page size 20 → 3 pages, numbered.
        for i in range(45):
            Claim.objects.create(
                client_email=f'p{i}@e.com', alf_claim_id=f'ALFPG{i:04d}',
                status='Claim submitted', status_category='open')
        html = self.web.get(self.URL + '?tab=all').content.decode()
        self.assertIn('aria-current="page"', html)        # current page highlighted
        self.assertIn('&page=2', html)                    # a numbered link
        self.assertIn('&page=3', html)
        # Numbers carry the active tab through.
        self.assertIn('?tab=all&page=2', html)

    def test_pagination_preserves_search_and_date(self):
        for i in range(45):
            Claim.objects.create(
                client_email=f'q{i}@e.com', client_name='Page Person',
                alf_claim_id=f'ALFQ{i:04d}', status_category='open')
        html = self.web.get(self.URL + '?tab=all&search=Page+Person').content.decode()
        self.assertIn('search=Page%20Person', html)       # search rides the page links, URL-encoded
        self.assertIn('&page=2', html)

    def test_date_filter_shows_only_that_day(self):
        # created_at is auto-set; rewrite it to place claims on distinct days.
        on_day = Claim.objects.create(
            client_email='onday@e.com', client_name='On The Day',
            alf_claim_id='ALFDAY1', status_category='open')
        off_day = Claim.objects.create(
            client_email='offday@e.com', client_name='Other Day',
            alf_claim_id='ALFDAY2', status_category='open')
        target = timezone.now().replace(year=2026, month=3, day=15)
        Claim.objects.filter(pk=on_day.pk).update(created_at=target)
        Claim.objects.filter(pk=off_day.pk).update(
            created_at=target - timedelta(days=5))

        html = self.web.get(self.URL + '?tab=all&date=2026-03-15').content.decode()
        self.assertIn('On The Day', html)
        self.assertNotIn('Other Day', html)
        # A clear-date control is offered.
        self.assertIn('Clear date', html)

    def test_bad_date_is_ignored(self):
        resp = self.web.get(self.URL + '?tab=all&date=not-a-date')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Ana Popescu', resp.content.decode())  # unfiltered
