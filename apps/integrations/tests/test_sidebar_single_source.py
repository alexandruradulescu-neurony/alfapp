"""Single source of truth between LORA app and the Zendesk sidebar (2026-06-13).

The sidebar briefing must show the SAME stored claim.ai_summary the app shows
— not generate its own — and must NOT regenerate on every open. It regenerates
(and persists) only on an explicit refresh. Claimless tickets still get a
transient briefing (nothing to store against).
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.config.models import SystemSettings

SECRET = 'sidebar-single-source-secret'


class BriefingSingleSourceTests(TestCase):
    URL = '/api/integrations/zd/briefing/'

    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.sidebar_secret_token = SECRET
        ss.save()
        cache.clear()
        self.addCleanup(cache.clear)
        self.api = APIClient()
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {SECRET}'}
        self.claim = Claim.objects.create(
            client_email='c@example.com', zd_ticket_id='81001',
            alf_claim_id='ALF8100100', status='Pending', status_category='pending',
            ai_summary='THE ONE STORED SUMMARY', ai_summary_updated_at=timezone.now())

    def test_open_returns_stored_summary_without_regenerating(self):
        with patch('apps.integrations.briefing.refresh_claim_summary') as refresh:
            resp = self.api.post(self.URL, {'ticket_id': '81001'},
                                 format='json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['summary'], 'THE ONE STORED SUMMARY')
        self.assertTrue(resp.data['stored'])
        self.assertIsNotNone(resp.data['summary_updated_at'])
        refresh.assert_not_called()  # opening a ticket must NOT regenerate

    def test_refresh_regenerates_and_persists(self):
        def fake_refresh(claim, ticket_data):
            claim.ai_summary = 'REGENERATED SUMMARY'
            claim.ai_summary_updated_at = timezone.now()
            claim.save(update_fields=['ai_summary', 'ai_summary_updated_at'])
            return True
        with patch('apps.integrations.briefing.refresh_claim_summary',
                   side_effect=fake_refresh) as refresh:
            resp = self.api.post(self.URL, {'ticket_id': '81001', 'refresh': True},
                                 format='json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        refresh.assert_called_once()
        self.assertEqual(resp.data['summary'], 'REGENERATED SUMMARY')
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.ai_summary, 'REGENERATED SUMMARY')

    def test_missing_summary_is_generated_once_on_open(self):
        self.claim.ai_summary = ''
        self.claim.save(update_fields=['ai_summary'])
        with patch('apps.integrations.briefing.refresh_claim_summary',
                   return_value=True) as refresh:
            self.api.post(self.URL, {'ticket_id': '81001'}, format='json', **self.auth)
        refresh.assert_called_once()  # no stored summary yet → generate+store

    def test_emails_endpoint_lists_the_stored_emails(self):
        from apps.communications.models import EmailLog
        EmailLog.objects.create(
            claim=self.claim, subject='Found it', body='b', from_email='lf@x.com',
            category='OBJECT_FOUND', action_required=True, auto_resolved=False)
        resp = self.api.post('/api/integrations/zd/emails/',
                             {'ticket_id': '81001'}, format='json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['emails']), 1)
        e = resp.data['emails'][0]
        self.assertEqual(e['subject'], 'Found it')
        self.assertTrue(e['action_required'])
        self.assertFalse(resp.data['claimless'])

    def test_emails_endpoint_requires_secret(self):
        resp = self.api.post('/api/integrations/zd/emails/', {'ticket_id': '81001'},
                             format='json')
        self.assertEqual(resp.status_code, 403)

    def test_claimless_ticket_still_gets_transient_briefing(self):
        from apps.ai.schemas import BriefingSummary
        with patch('apps.ai.client.AIClient.complete',
                   return_value=BriefingSummary(summary='transient', next_steps=[])):
            resp = self.api.post(self.URL, {'ticket_id': '99999', 'subject': 'x',
                                            'description': 'y'},
                                 format='json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['summary'], 'transient')
        self.assertFalse(resp.data['stored'])
