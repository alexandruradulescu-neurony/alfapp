"""Manual 'Mark resolved' for action-required emails (2026-06-12).

Institution mail that needs a human (e.g. Object Found) is flagged
action_required and had no way to be cleared — it stayed flagged forever and
kept inflating the manager dashboard's 'Emails need a reply' count. The
resolve action is a LORA-side toggle that clears (or restores) that flag.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.communications.models import EmailLog

User = get_user_model()


class EmailResolveTests(TestCase):
    def setUp(self):
        self.agent = User.objects.create_user(
            username='resolve_agent', password='x')
        self.api = APIClient()
        self.claim = Claim.objects.create(
            client_email='c@example.com', zd_ticket_id='78001')
        self.email = EmailLog.objects.create(
            claim=self.claim, subject='Object found', body='We found it',
            category='OBJECT_FOUND', action_required=True, auto_resolved=False)
        self.url = f'/api/communications/email-logs/{self.email.id}/resolve/'

    def test_requires_auth(self):
        resp = APIClient().post(self.url, {'resolved': True}, format='json')
        self.assertIn(resp.status_code, (401, 403))

    def test_mark_resolved_clears_flag(self):
        self.api.force_authenticate(self.agent)
        resp = self.api.post(self.url, {'resolved': True}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['action_required'])
        self.email.refresh_from_db()
        self.assertFalse(self.email.action_required)

    def test_reopen_restores_flag(self):
        self.email.action_required = False
        self.email.save(update_fields=['action_required'])
        self.api.force_authenticate(self.agent)
        resp = self.api.post(self.url, {'resolved': False}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.email.refresh_from_db()
        self.assertTrue(self.email.action_required)

    def test_resolving_drops_it_from_attention_count(self):
        # The manager dashboard counts action_required + not auto_resolved.
        from django.db.models import Q
        attention = lambda: Claim.objects.filter(
            emails__action_required=True, emails__auto_resolved=False).distinct().count()
        self.assertEqual(attention(), 1)
        self.api.force_authenticate(self.agent)
        self.api.post(self.url, {'resolved': True}, format='json')
        self.assertEqual(attention(), 0)
