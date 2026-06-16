"""Client follow-up update cadence (day 2/5/11/21): scheduling, per-office-aware
drafting, and the agent prepare/send/skip actions (2026-06-14)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications.models import EmailLog, ClientUpdate
from apps.communications import client_updates as cu

User = get_user_model()


class ScheduleTests(TestCase):
    def test_cascade_creates_only_the_next(self):
        claim = Claim.objects.create(client_email='a@example.com')
        anchor = timezone.now()
        first = cu.schedule_next(claim, anchor)
        self.assertEqual(first.milestone, 'DAY_2')
        self.assertEqual((first.due_at - anchor).days, 2)
        # idempotent — while one update is open, no new one is created
        self.assertIsNone(cu.schedule_next(claim, anchor))
        self.assertEqual(claim.follow_up_updates.count(), 1)

    def test_cascade_advances_after_resolution(self):
        claim = Claim.objects.create(client_email='a@example.com')
        anchor = timezone.now()
        d2 = cu.schedule_next(claim, anchor)
        d2.state = 'SENT'
        d2.sent_at = timezone.now()
        d2.save()
        nxt = cu.schedule_next(claim)  # anchor re-derived from DAY_2
        self.assertEqual(nxt.milestone, 'DAY_5')
        self.assertEqual((nxt.due_at - anchor).days, 5)

    def test_cancel_open_skips_unsent_only(self):
        claim = Claim.objects.create(client_email='a@example.com')
        d2 = cu.schedule_next(claim, timezone.now())
        d2.state = 'SENT'
        d2.sent_at = timezone.now()
        d2.save()
        cu.schedule_next(claim)  # DAY_5 now open
        cu.cancel_open_follow_ups(claim)
        self.assertEqual(set(claim.follow_up_updates.values_list('state', flat=True)), {'SENT', 'SKIPPED'})

    def test_due_returns_only_past_scheduled(self):
        claim = Claim.objects.create(client_email='a@example.com')
        cu.schedule_next(claim, timezone.now() - timedelta(days=6))
        self.assertEqual({u.milestone for u in cu.due_follow_ups(claim)}, {'DAY_2'})


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

    def test_negative_only_replies_are_no_news_and_never_drafted(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.ai_api_key = 'k'
        ss.save()
        claim = Claim.objects.create(client_email='a@example.com', client_name='Lee', object_description='bag')
        not_found = EmailLog.objects.create(claim=claim, from_email='a@b.gov', subject='no',
                                            body='x', category='OBJECT_NOT_FOUND',
                                            ai_summary='We did not find it; case closed.')
        expired = EmailLog.objects.create(claim=claim, from_email='c@d.gov', subject='expired',
                                          body='x', category='RESUBMISSION_REQUIRED',
                                          ai_summary='Your submission expired.')
        with patch('apps.ai.client.AIClient.complete') as ai:
            body, has_news = cu._draft_follow_up(claim, [not_found, expired])
        ai.assert_not_called()                      # harmful text never reaches the model
        self.assertFalse(has_news)
        self.assertNotIn('expired', body.lower())
        self.assertNotIn('case closed', body.lower())
        self.assertIn('still actively following up', body)


class StopConditionTests(TestCase):
    """claim_is_closed: what voids the cadence and what must NOT."""

    def _claim(self):
        return Claim.objects.create(client_email='a@example.com')

    def test_completed_refund_voids_but_pending_does_not(self):
        from decimal import Decimal
        from apps.payments.models import Refund
        claim = self._claim()
        r = Refund.objects.create(claim=claim, paypal_refund_id='R-PEND', amount=Decimal('5'),
                                  refund_type='FULL', reason='x', status='PENDING')
        self.assertFalse(cu.claim_is_closed(claim))   # a pending/failed refund must not stop updates
        r.status = 'COMPLETED'
        r.save()
        self.assertTrue(cu.claim_is_closed(claim))

    def test_open_dispute_voids_but_resolved_does_not(self):
        from apps.payments.models import Dispute
        claim = self._claim()
        d = Dispute.objects.create(claim=claim, paypal_dispute_id='D-OPEN', status='UNDER_REVIEW',
                                   buyer_email='a@example.com', transaction_date=timezone.now())
        self.assertTrue(cu.claim_is_closed(claim))
        d.status = 'RESOLVED_WON'
        d.save()
        self.assertFalse(cu.claim_is_closed(claim))   # a finished dispute no longer blocks


class PrepareSendSkipTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(client_email='a@example.com', client_name='Lee', zd_ticket_id='97001')
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))
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


class StartTests(TestCase):
    def test_start_drafts_initial_and_schedules(self):
        claim = Claim.objects.create(client_email='a@example.com', client_name='Lee', object_description='iPad')
        self.assertTrue(cu.start_client_updates(claim))
        claim.refresh_from_db()
        self.assertTrue(claim.client_report_draft)
        self.assertEqual(claim.follow_up_updates.count(), 1)  # cascade: only the first
        # idempotent — second call is a no-op
        self.assertFalse(cu.start_client_updates(claim))

    def test_start_view_initializes_claim(self):
        mgr = User.objects.create_user(username='start_mgr', password='x')
        web = Client()
        web.force_login(mgr)
        claim = Claim.objects.create(client_email='a@example.com', client_name='Lee', zd_ticket_id='99001')
        web.post(reverse('client_updates_start', args=[claim.id]))
        claim.refresh_from_db()
        self.assertTrue(claim.client_report_draft)
        self.assertEqual(claim.follow_up_updates.count(), 1)


class FollowupViewTests(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='fu_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)
        self.claim = Claim.objects.create(client_email='a@example.com', client_name='Lee', zd_ticket_id='97001')
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))
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


class CadenceTests(TestCase):
    """The cadence generator + cascade-to-FINAL, driven by service length."""

    def test_offsets_for_various_service_lengths(self):
        from apps.communications.constants import cadence_offsets
        self.assertEqual(cadence_offsets(30), [2, 5, 11, 21])
        self.assertEqual(cadence_offsets(35), [2, 5, 11, 21, 31])
        self.assertEqual(cadence_offsets(40), [2, 5, 11, 21, 31])
        self.assertEqual(cadence_offsets(45), [2, 5, 11, 21, 31, 41])
        self.assertEqual(cadence_offsets(55), [2, 5, 11, 21, 31, 41, 51])

    def test_short_service_truncates(self):
        from apps.communications.constants import cadence_offsets
        self.assertEqual(cadence_offsets(10), [2, 5])
        self.assertEqual(cadence_offsets(2), [])  # nothing strictly inside

    def test_cascade_walks_to_final_then_stops(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.service_length_days = 30
        ss.save()
        claim = Claim.objects.create(client_email='a@example.com')
        anchor = timezone.now()
        seq = []
        nxt = cu.schedule_next(claim, anchor)
        while nxt and len(seq) < 12:  # guard against an accidental infinite loop
            seq.append(nxt.milestone)
            nxt.state = 'SENT'
            nxt.sent_at = timezone.now()
            nxt.save()
            nxt = cu.schedule_next(claim)
        self.assertEqual(seq, ['DAY_2', 'DAY_5', 'DAY_11', 'DAY_21', 'FINAL'])
        final = claim.follow_up_updates.get(milestone='FINAL')
        self.assertEqual((final.due_at - claim.created_at).days, 30)

    def test_longer_service_extends_the_tail(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.service_length_days = 45
        ss.save()
        claim = Claim.objects.create(client_email='a@example.com')
        anchor = timezone.now()
        seq = []
        nxt = cu.schedule_next(claim, anchor)
        while nxt and len(seq) < 12:
            seq.append(nxt.milestone)
            nxt.state = 'SENT'
            nxt.save()
            nxt = cu.schedule_next(claim)
        self.assertEqual(seq, ['DAY_2', 'DAY_5', 'DAY_11', 'DAY_21', 'DAY_31', 'DAY_41', 'FINAL'])

    def test_final_never_due_before_last_cadence_when_submission_lags(self):
        # Submission lags creation, and L sits just above a tail boundary — the
        # raw FINAL date would precede the last cadence update; it must be clamped.
        from apps.communications.client_updates import cadence_plan
        creation = timezone.now()
        submission = creation + timedelta(days=5)   # agent investigated for 5 days first
        plan = dict(cadence_plan(submission, creation, 35))   # tail = DAY_31
        self.assertGreater(plan['FINAL'], plan['DAY_31'])


class RunnerTests(TestCase):
    """The autonomous runner (run_due_updates) behind the autosend flag."""

    def setUp(self):
        from apps.config.models import SystemSettings
        self.ss = SystemSettings.get_instance()
        self.ss.client_updates_autosend = True
        self.ss.ai_api_key = ''           # template-only drafting, no live AI
        self.ss.service_length_days = 30
        self.ss.save()

    def _due_claim(self):
        claim = Claim.objects.create(client_email='a@example.com', client_name='Lee',
                                     object_description='iPad', zd_ticket_id='97001')
        cu.schedule_next(claim, timezone.now() - timedelta(days=3))  # DAY_2 due
        return claim

    def test_autosend_off_is_noop(self):
        self.ss.client_updates_autosend = False
        self.ss.save()
        self._due_claim()
        with patch('apps.integrations.services.post_zendesk_comment') as post:
            result = cu.run_due_updates()
        post.assert_not_called()
        self.assertFalse(result['enabled'])

    def test_sends_due_update_and_advances(self):
        claim = self._due_claim()
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}) as post:
            result = cu.run_due_updates()
        post.assert_called_once()
        self.assertFalse(post.call_args.kwargs.get('is_internal'))  # PUBLIC reply
        self.assertEqual(result['sent'], 1)
        self.assertEqual(claim.follow_up_updates.get(milestone='DAY_2').state, 'SENT')
        # cascade advanced
        self.assertTrue(claim.follow_up_updates.filter(milestone='DAY_5', state='SCHEDULED').exists())

    def test_object_found_is_held_for_an_agent(self):
        claim = self._due_claim()
        EmailLog.objects.create(claim=claim, from_email='lf@airport.gov', subject='Found it',
                                body='match', category='OBJECT_FOUND')
        with patch('apps.integrations.services.post_zendesk_comment') as post:
            result = cu.run_due_updates()
        post.assert_not_called()                       # never auto-send a "found"
        self.assertEqual(result['held'], 1)
        self.assertEqual(claim.follow_up_updates.get(milestone='DAY_2').state, 'DRAFTED')
        # cascade paused on the open draft
        self.assertFalse(claim.follow_up_updates.filter(milestone='DAY_5').exists())

    def test_final_sent_when_not_found(self):
        claim = Claim.objects.create(client_email='a@example.com', client_name='Lee',
                                     object_description='iPad', zd_ticket_id='97001')
        ClientUpdate.objects.create(claim=claim, milestone='FINAL', state='SCHEDULED',
                                    due_at=timezone.now() - timedelta(hours=1))
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}) as post:
            result = cu.run_due_updates()
        post.assert_called_once()
        self.assertIn('trusting us', post.call_args.args[1].lower())
        self.assertEqual(result['sent'], 1)

    def test_final_held_for_agent_when_found(self):
        # A stray "found" must NOT silently suppress the end-of-service note —
        # it is held for a human to decide, never auto-sent and never dropped.
        claim = Claim.objects.create(client_email='a@example.com', client_name='Lee', zd_ticket_id='97001')
        fin = ClientUpdate.objects.create(claim=claim, milestone='FINAL', state='SCHEDULED',
                                          due_at=timezone.now() - timedelta(hours=1))
        EmailLog.objects.create(claim=claim, from_email='lf@airport.gov', subject='Found it',
                                body='match', category='OBJECT_FOUND')
        with patch('apps.integrations.services.post_zendesk_comment') as post:
            result = cu.run_due_updates()
        post.assert_not_called()
        fin.refresh_from_db()
        self.assertEqual(fin.state, 'DRAFTED')
        self.assertEqual(result['held'], 1)

    def test_negative_housekeeping_not_relayed_in_autonomous_mode(self):
        # A "submission expired" notice must never reach the client, even with AI
        # configured and autosend on. The AI is not even called for negative-only.
        self.ss.ai_api_key = 'k'
        self.ss.save()
        claim = Claim.objects.create(client_email='a@example.com', client_name='Lee',
                                     object_description='iPad', zd_ticket_id='97001')
        cu.schedule_next(claim, timezone.now() - timedelta(days=3))
        EmailLog.objects.create(claim=claim, from_email='lf@airport.gov', subject='Submission expired',
                                body='Your submission expired; case closed on our side.',
                                category='RESUBMISSION_REQUIRED',
                                ai_summary='Submission expired; case closed on our side.')
        with patch('apps.ai.client.AIClient.complete') as ai, \
             patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}) as post:
            result = cu.run_due_updates()
        ai.assert_not_called()
        post.assert_called_once()
        body = post.call_args.args[1].lower()
        self.assertNotIn('expired', body)
        self.assertNotIn('case closed', body)
        self.assertIn('still actively following up', body)
        self.assertEqual(result['sent'], 1)

    def test_failed_send_reverts_to_scheduled_for_retry(self):
        claim = self._due_claim()
        with patch('apps.integrations.services.post_zendesk_comment', return_value=None) as post:
            result = cu.run_due_updates()
        post.assert_called_once()
        self.assertEqual(result['failed'], 1)
        self.assertEqual(claim.follow_up_updates.get(milestone='DAY_2').state, 'SCHEDULED')
        # the next run retries and succeeds
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}):
            result2 = cu.run_due_updates()
        self.assertEqual(result2['sent'], 1)

    def test_closed_claim_cancels_cadence(self):
        claim = self._due_claim()
        claim.status_category = 'solved'
        claim.save()
        with patch('apps.integrations.services.post_zendesk_comment') as post:
            cu.run_due_updates()
        post.assert_not_called()
        self.assertEqual(claim.follow_up_updates.get(milestone='DAY_2').state, 'SKIPPED')

    def test_open_dispute_stops_cadence(self):
        from apps.payments.models import Dispute
        claim = self._due_claim()
        Dispute.objects.create(claim=claim, paypal_dispute_id='PP-D-1', status='RECEIVED',
                               buyer_email='lee@example.com', transaction_date=timezone.now())
        with patch('apps.integrations.services.post_zendesk_comment') as post:
            cu.run_due_updates()
        post.assert_not_called()
        self.assertEqual(claim.follow_up_updates.get(milestone='DAY_2').state, 'SKIPPED')
