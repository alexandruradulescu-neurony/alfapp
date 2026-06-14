"""Client follow-up update cadence (day 2/5/11/21): scheduling, per-office-aware
drafting, and the agent prepare/send/skip actions (2026-06-14)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.communications import client_updates as cu

User = get_user_model()


class ScheduleTests(TestCase):
    def test_schedule_creates_four_idempotent(self):
        claim = Claim.objects.create(client_email='a@example.com')
        anchor = timezone.now()
        cu.schedule_follow_ups(claim, anchor)
        cu.schedule_follow_ups(claim, anchor)  # second call must not duplicate
        ups = list(claim.follow_up_updates.order_by('due_at'))
        self.assertEqual([u.milestone for u in ups], ['DAY_2', 'DAY_5', 'DAY_11', 'DAY_21'])
        self.assertEqual((ups[0].due_at - anchor).days, 2)
        self.assertEqual((ups[3].due_at - anchor).days, 21)

    def test_cancel_open_skips_unsent_only(self):
        claim = Claim.objects.create(client_email='a@example.com')
        cu.schedule_follow_ups(claim, timezone.now())
        sent = claim.follow_up_updates.first()
        sent.state = 'SENT'
        sent.save()
        cu.cancel_open_follow_ups(claim)
        self.assertEqual(set(claim.follow_up_updates.values_list('state', flat=True)), {'SENT', 'SKIPPED'})

    def test_due_returns_only_past_scheduled(self):
        claim = Claim.objects.create(client_email='a@example.com')
        cu.schedule_follow_ups(claim, timezone.now() - timedelta(days=6))
        self.assertEqual({u.milestone for u in cu.due_follow_ups(claim)}, {'DAY_2', 'DAY_5'})


class DraftTests(TestCase):
    def test_no_replies_uses_no_news_template(self):
        claim = Claim.objects.create(client_email='a@example.com', client_name='Lee', object_description='iPad')
        body, has_news = cu._draft_follow_up(claim, [])
        self.assertFalse(has_news)
        self.assertIn('still actively following up', body)
        self.assertIn('Lee', body)

    def test_multi_office_rule_is_in_prompt(self):
        prompt = cu.FOLLOWUP_SYSTEM_PROMPT.lower()
        self.assertIn('many offices', prompt)
        self.assertIn('not a verdict', prompt)
        self.assertIn('never', prompt)  # never promise recovery

    def test_replies_use_ai_when_configured(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'k'
        ss.save()
        claim = Claim.objects.create(client_email='a@example.com', client_name='Lee')
        rep = EmailLog.objects.create(claim=claim, from_email='den@lostfound.gov', subject='re',
                                      body='x', category='OBJECT_FOUND', ai_summary='Possible match found')

        class _Reply:
            body = 'AI progress body'

        with patch('apps.ai.client.AIClient.complete', return_value=_Reply()):
            body, has_news = cu._draft_follow_up(claim, [rep])
        self.assertEqual(body, 'AI progress body')
        self.assertTrue(has_news)


class PrepareSendSkipTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(client_email='a@example.com', client_name='Lee', zd_ticket_id='97001')
        cu.schedule_follow_ups(self.claim, timezone.now() - timedelta(days=3))
        self.update = self.claim.follow_up_updates.get(milestone='DAY_2')

    def test_prepare_sets_drafted(self):
        with patch.object(cu, '_draft_follow_up', return_value=('DRAFT BODY', True)):
            cu.prepare_follow_up(self.update, fetch_email=False)
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, 'DRAFTED')
        self.assertEqual(self.update.draft_body, 'DRAFT BODY')

    def test_send_posts_public_reply_and_marks_sent(self):
        self.update.state = 'DRAFTED'
        self.update.save()
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}) as post:
            ok = cu.send_follow_up(self.update, 'Final body for Lee.')
        self.assertTrue(ok)
        args, kwargs = post.call_args
        self.assertEqual(args[0], '97001')
        self.assertFalse(kwargs.get('is_internal'))   # PUBLIC reply
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, 'SENT')
        self.assertEqual(self.update.draft_body, 'Final body for Lee.')

    def test_send_blocked_without_ticket(self):
        self.claim.zd_ticket_id = ''
        self.claim.save()
        with patch('apps.integrations.services.post_zendesk_comment') as post:
            self.assertFalse(cu.send_follow_up(self.update, 'x'))
        post.assert_not_called()

    def test_skip(self):
        cu.skip_follow_up(self.update)
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, 'SKIPPED')


class FollowupViewTests(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='fu_mgr', password='x', role='MANAGER')
        self.web = Client()
        self.web.force_login(self.mgr)
        self.claim = Claim.objects.create(client_email='a@example.com', client_name='Lee', zd_ticket_id='97001')
        cu.schedule_follow_ups(self.claim, timezone.now() - timedelta(days=3))
        self.update = self.claim.follow_up_updates.get(milestone='DAY_2')

    def test_prepare_view(self):
        with patch.object(cu, '_draft_follow_up', return_value=('B', True)):
            resp = self.web.post(reverse('client_followup_prepare', args=[self.update.id]))
        self.assertRedirects(resp, reverse('agent_claim_detail', args=[self.claim.id]),
                             fetch_redirect_response=False)
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, 'DRAFTED')

    def test_send_view(self):
        self.update.state = 'DRAFTED'
        self.update.save()
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}):
            self.web.post(reverse('client_followup_send', args=[self.update.id]), {'body': 'Hi Lee'})
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, 'SENT')

    def test_skip_view(self):
        self.web.post(reverse('client_followup_skip', args=[self.update.id]))
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, 'SKIPPED')
