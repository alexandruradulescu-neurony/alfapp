"""The resolve control must exist on the Emails screen (list + single email),
not only on the claim page — that's where agents/managers work email."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.claims.models import Claim
from apps.communications.models import EmailLog

User = get_user_model()


class EmailScreenResolveTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(
            username='emailscreen_mgr', password='x', role='MANAGER')
        self.web = Client()
        self.web.force_login(self.manager)
        self.claim = Claim.objects.create(client_email='c@example.com', zd_ticket_id='79001')
        self.needs = EmailLog.objects.create(
            claim=self.claim, subject='Needs reply', body='b',
            category='OBJECT_FOUND', action_required=True, auto_resolved=False)
        self.done = EmailLog.objects.create(
            claim=self.claim, subject='All handled', body='b',
            category='GENERAL_CORRESPONDENCE', action_required=False, auto_resolved=False)

    def test_detail_page_shows_resolve_for_action_required(self):
        resp = self.web.get(f'/agent/emails/{self.needs.id}/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('Mark resolved', html)
        self.assertIn(f'resolveEmail({self.needs.id}, true)', html)

    def test_detail_page_shows_reopen_when_not_action_required(self):
        resp = self.web.get(f'/agent/emails/{self.done.id}/')
        html = resp.content.decode()
        self.assertIn('needs attention', html)
        self.assertIn(f'resolveEmail({self.done.id}, false)', html)

    def test_list_shows_resolve_on_action_required_rows(self):
        resp = self.web.get('/agent/emails/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(f'resolveEmail({self.needs.id}, true)', html)
