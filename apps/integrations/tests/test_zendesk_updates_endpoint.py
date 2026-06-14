"""Sidebar /zd/updates/ endpoint — auth, timeline, and actions (2026-06-14)."""

import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.communications import client_updates as cu

SECRET = 'sidebar-test-secret'


class ZendeskUpdatesEndpointTests(TestCase):
    def setUp(self):
        ss = SystemSettings.get_instance()
        ss.sidebar_secret_token = SECRET
        ss.save()
        self.url = reverse('zendesk-client-updates')
        self.web = Client()
        self.claim = Claim.objects.create(
            client_email='lee@example.com', client_name='Lee Foley', alf_claim_id='ALF1',
            zd_ticket_id='97001', client_report_draft='Dear Lee, here is what we did...')
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))

    def _post(self, body, auth=True):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {SECRET}'} if auth else {}
        return self.web.post(self.url, data=json.dumps(body),
                             content_type='application/json', **headers)

    def test_rejects_bad_secret(self):
        resp = self.web.post(self.url, data=json.dumps({'ticket_id': '97001'}),
                             content_type='application/json', HTTP_AUTHORIZATION='Bearer wrong')
        self.assertEqual(resp.status_code, 403)

    def test_no_claim_returns_empty(self):
        resp = self._post({'ticket_id': 'nope'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()['claim'])

    def test_timeline_lists_initial_and_followups(self):
        resp = self._post({'ticket_id': '97001'})
        data = resp.json()
        self.assertTrue(data['claim'])
        kinds = [it['kind'] for it in data['items']]
        self.assertEqual(kinds.count('initial'), 1)
        # Cascade: only the next (DAY_2) follow-up is created up front.
        self.assertEqual(kinds.count('followup'), 1)
        due = [it for it in data['items'] if it.get('is_due')]
        self.assertEqual({it['milestone'] for it in due}, {'DAY_2'})

    def test_skip_action_marks_skipped(self):
        fu = self.claim.follow_up_updates.get(milestone='DAY_2')
        resp = self._post({'ticket_id': '97001', 'action': 'skip', 'kind': 'followup', 'id': fu.id})
        self.assertEqual(resp.status_code, 200)
        fu.refresh_from_db()
        self.assertEqual(fu.state, 'SKIPPED')

    def test_start_action_initializes_empty_claim(self):
        bare = Claim.objects.create(client_email='x@example.com', client_name='Bo', zd_ticket_id='98002')
        resp = self._post({'ticket_id': '98002', 'action': 'start'})
        data = resp.json()
        self.assertTrue(data['claim'])
        self.assertTrue(any(it['kind'] == 'initial' for it in data['items']))
        bare.refresh_from_db()
        # Cascade: starting only creates the first follow-up.
        self.assertEqual(bare.follow_up_updates.count(), 1)

    def test_send_initial_posts_public_reply(self):
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}) as post:
            self._post({'ticket_id': '97001', 'action': 'send', 'kind': 'initial',
                        'body': 'Edited initial message for Lee.'})
        self.claim.refresh_from_db()
        post.assert_called_once()
        self.assertFalse(post.call_args.kwargs.get('is_internal'))
        self.assertIsNotNone(self.claim.client_report_sent_at)
