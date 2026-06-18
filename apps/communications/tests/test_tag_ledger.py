"""Tag-ledger integration tests (TDD / RED-first).

Tests:
  1. tag_for_milestone: ordinals at the default 30-day service.
  2. tag_for_milestone: ordinals shift when service is extended (e.g. 32 days inserts DAY_31).
  3. send_follow_up on a non-FINAL milestone: adds client_update_N + removes attention pair; marks SENT.
  4. send_follow_up on FINAL: also adds the three terminal tags; does NOT change ticket status.
  5. send_follow_up paused when claim.risk_active: no post, no tag calls.
  6. run_due_updates READ-before: tag already present -> skip without posting; schedule_next advances.
  7. run_due_updates tag absent -> normal send path (tag written).
"""

from datetime import timedelta
from unittest.mock import call, patch, MagicMock

from django.test import TestCase
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications.models import ClientUpdate
from apps.communications import client_updates as cu
from apps.communications.constants import (
    FINAL_MILESTONE,
    CLIENT_UPDATE_TAG_PREFIX,
    ATTENTION_TAGS,
    FINAL_TERMINAL_TAGS,
)


# ---------------------------------------------------------------------------
# 1 & 2: tag_for_milestone ordinals
# ---------------------------------------------------------------------------

class TagForMilestoneDefaultServiceTest(TestCase):
    """At 30-day service: DAY_2->1, DAY_5->2, DAY_11->3, DAY_21->4, FINAL->5."""

    def setUp(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.service_length_days = 30
        ss.save()
        self.claim = Claim.objects.create(client_email='a@example.com')

    def test_day2_is_client_update_1(self):
        self.assertEqual(cu.tag_for_milestone(self.claim, 'DAY_2'), 'client_update_1')

    def test_day5_is_client_update_2(self):
        self.assertEqual(cu.tag_for_milestone(self.claim, 'DAY_5'), 'client_update_2')

    def test_day11_is_client_update_3(self):
        self.assertEqual(cu.tag_for_milestone(self.claim, 'DAY_11'), 'client_update_3')

    def test_day21_is_client_update_4(self):
        self.assertEqual(cu.tag_for_milestone(self.claim, 'DAY_21'), 'client_update_4')

    def test_final_is_client_update_5(self):
        self.assertEqual(cu.tag_for_milestone(self.claim, FINAL_MILESTONE), 'client_update_5')

    def test_unknown_milestone_returns_empty_string(self):
        self.assertEqual(cu.tag_for_milestone(self.claim, 'DAY_99'), '')


class TagForMilestoneExtendedServiceTest(TestCase):
    """At 32-day service: DAY_31 inserted -> DAY_31='client_update_5', FINAL='client_update_6'."""

    def setUp(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.service_length_days = 32
        ss.save()
        self.claim = Claim.objects.create(client_email='a@example.com')

    def test_day31_is_client_update_5(self):
        self.assertEqual(cu.tag_for_milestone(self.claim, 'DAY_31'), 'client_update_5')

    def test_final_is_client_update_6_when_extended(self):
        self.assertEqual(cu.tag_for_milestone(self.claim, FINAL_MILESTONE), 'client_update_6')


# ---------------------------------------------------------------------------
# 3: send_follow_up non-FINAL: adds tag + removes attention pair; marks SENT
# ---------------------------------------------------------------------------

class SendFollowUpTagWriteTest(TestCase):
    """send_follow_up writes the ledger tag and removes the attention pair."""

    def setUp(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.service_length_days = 30
        ss.save()
        self.claim = Claim.objects.create(
            client_email='a@example.com', client_name='Lee', zd_ticket_id='97001')
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))
        self.update = self.claim.follow_up_updates.get(milestone='DAY_2')
        self.update.state = ClientUpdate.STATE_DRAFTED
        self.update.save()

    def test_add_and_remove_tags_on_successful_send(self):
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}), \
             patch('apps.communications.client_updates.add_zendesk_ticket_tags') as mock_add, \
             patch('apps.communications.client_updates.remove_zendesk_ticket_tags') as mock_remove:
            ok = cu.send_follow_up(self.update, 'Hello Lee, still searching.')

        self.assertTrue(ok)
        # add called with the milestone tag (client_update_1 for DAY_2 at 30 days)
        mock_add.assert_called_once_with('97001', ['client_update_1'])
        # remove called with the attention pair
        mock_remove.assert_called_once_with('97001', list(ATTENTION_TAGS))

    def test_update_marked_sent(self):
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}), \
             patch('apps.communications.client_updates.add_zendesk_ticket_tags'), \
             patch('apps.communications.client_updates.remove_zendesk_ticket_tags'):
            cu.send_follow_up(self.update, 'Hello Lee.')
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, ClientUpdate.STATE_SENT)

    def test_no_tags_written_when_post_fails(self):
        """If Zendesk post returns None (failure), no tags should be written."""
        with patch('apps.integrations.services.post_zendesk_comment', return_value=None), \
             patch('apps.communications.client_updates.add_zendesk_ticket_tags') as mock_add, \
             patch('apps.communications.client_updates.remove_zendesk_ticket_tags') as mock_remove:
            ok = cu.send_follow_up(self.update, 'Hello Lee.')
        self.assertFalse(ok)
        mock_add.assert_not_called()
        mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 4: send_follow_up on FINAL: terminal tags added; no status change
# ---------------------------------------------------------------------------

class SendFollowUpFinalTagTest(TestCase):
    """FINAL send adds client_update_N + the three terminal tags; never changes ticket status."""

    def setUp(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.service_length_days = 30
        ss.save()
        self.claim = Claim.objects.create(
            client_email='a@example.com', client_name='Lee', zd_ticket_id='97001')
        self.update = ClientUpdate.objects.create(
            claim=self.claim, milestone=FINAL_MILESTONE, state=ClientUpdate.STATE_DRAFTED,
            due_at=timezone.now() - timedelta(hours=1))

    def test_final_adds_terminal_tags(self):
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}), \
             patch('apps.communications.client_updates.add_zendesk_ticket_tags') as mock_add, \
             patch('apps.communications.client_updates.remove_zendesk_ticket_tags'):
            ok = cu.send_follow_up(self.update, 'End of service.')

        self.assertTrue(ok)
        calls = mock_add.call_args_list
        # First call: the milestone tag (client_update_5 for FINAL at 30 days)
        self.assertEqual(calls[0], call('97001', ['client_update_5']))
        # Second call: the terminal tags
        self.assertEqual(calls[1], call('97001', list(FINAL_TERMINAL_TAGS)))

    def test_final_does_not_change_ticket_status(self):
        """send_follow_up must NEVER change ticket status.

        send_follow_up has no status-change code today — confirm that the update
        itself is left in STATE_SENT (not anything else) and that claim.status_category
        is untouched after the send. This test protects against a future regression
        where someone adds a close/solve call inside send_follow_up.
        """
        original_status = self.claim.status_category
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}), \
             patch('apps.communications.client_updates.add_zendesk_ticket_tags'), \
             patch('apps.communications.client_updates.remove_zendesk_ticket_tags'):
            ok = cu.send_follow_up(self.update, 'End of service.')
        self.assertTrue(ok)
        self.update.refresh_from_db()
        self.assertEqual(self.update.state, ClientUpdate.STATE_SENT)
        # claim status_category must not have been changed
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status_category, original_status)


# ---------------------------------------------------------------------------
# 5: send_follow_up paused when risk_active: no post, no tags
# ---------------------------------------------------------------------------

class SendFollowUpRiskPauseTagTest(TestCase):
    """Existing risk-pause behaviour: no post, no tags when claim.risk_active."""

    def setUp(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.service_length_days = 30
        ss.save()
        self.claim = Claim.objects.create(
            client_email='a@example.com', zd_ticket_id='ZD-RISK',
            alf_claim_id='ALF0001', client_name='Risk Client', object_description='bag')
        self.claim.register_risk(reasons=['refund_demanded'], level='at_risk', detail='x')
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))
        self.update = self.claim.follow_up_updates.get(milestone='DAY_2')
        self.update.state = ClientUpdate.STATE_DRAFTED
        self.update.save()

    def test_risk_active_no_post_no_tags(self):
        with patch('apps.integrations.services.post_zendesk_comment') as mock_post, \
             patch('apps.communications.client_updates.add_zendesk_ticket_tags') as mock_add, \
             patch('apps.communications.client_updates.remove_zendesk_ticket_tags') as mock_remove:
            ok = cu.send_follow_up(self.update, 'Hello.')

        self.assertFalse(ok)
        mock_post.assert_not_called()
        mock_add.assert_not_called()
        mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# 6: run_due_updates READ-before: tag present -> skip without posting
# ---------------------------------------------------------------------------

class RunDueUpdatesReadBeforeTest(TestCase):
    """When the tag is already on the ticket, mark SKIPPED (not SENT via post) and advance."""

    def setUp(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.client_updates_autosend = True
        ss.ai_api_key = ''
        ss.service_length_days = 30
        ss.save()
        self.claim = Claim.objects.create(
            client_email='a@example.com', client_name='Lee',
            object_description='iPad', zd_ticket_id='97001')
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))  # DAY_2 due
        self.update = self.claim.follow_up_updates.get(milestone='DAY_2')

    def test_tag_present_skips_post_and_advances(self):
        """Tag 'client_update_1' already on ticket -> mark done without posting; schedule_next runs."""
        # DAY_2 at 30 days -> tag 'client_update_1'
        with patch('apps.communications.client_updates.get_zendesk_ticket_tags',
                   return_value=['client_update_1', 'some_other_tag']), \
             patch('apps.integrations.services.post_zendesk_comment') as mock_post, \
             patch('apps.communications.client_updates.add_zendesk_ticket_tags') as mock_add, \
             patch('apps.communications.client_updates.remove_zendesk_ticket_tags') as mock_remove:
            result = cu.run_due_updates()

        mock_post.assert_not_called()
        mock_add.assert_not_called()
        mock_remove.assert_not_called()
        # Update state must NOT be SENT (it was already done manually)
        self.update.refresh_from_db()
        self.assertNotEqual(self.update.state, ClientUpdate.STATE_SENT)
        # Cascade advanced: DAY_5 should now be scheduled
        self.assertTrue(
            self.claim.follow_up_updates.filter(milestone='DAY_5', state='SCHEDULED').exists(),
            "schedule_next should have created DAY_5 after the already-tagged skip"
        )

    def test_tag_present_result_counts_correctly(self):
        """Runner result should reflect 'already_tagged' / skipped update."""
        with patch('apps.communications.client_updates.get_zendesk_ticket_tags',
                   return_value=['client_update_1']), \
             patch('apps.integrations.services.post_zendesk_comment'), \
             patch('apps.communications.client_updates.add_zendesk_ticket_tags'), \
             patch('apps.communications.client_updates.remove_zendesk_ticket_tags'):
            result = cu.run_due_updates()
        # sent = 0 (nothing posted); skipped or already_tagged counter > 0
        self.assertEqual(result['sent'], 0)
        self.assertGreater(result.get('already_tagged', result.get('skipped', 0)), 0)


# ---------------------------------------------------------------------------
# 7: run_due_updates tag absent -> normal send path (tags written)
# ---------------------------------------------------------------------------

class RunDueUpdatesTagAbsentTest(TestCase):
    """Tag not on ticket -> normal path: send happens and tag is written."""

    def setUp(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.client_updates_autosend = True
        ss.ai_api_key = ''
        ss.service_length_days = 30
        ss.save()
        self.claim = Claim.objects.create(
            client_email='a@example.com', client_name='Lee',
            object_description='iPad', zd_ticket_id='97001')
        cu.schedule_next(self.claim, timezone.now() - timedelta(days=3))  # DAY_2 due

    def test_tag_absent_normal_send_and_writes_tag(self):
        with patch('apps.communications.client_updates.get_zendesk_ticket_tags',
                   return_value=[]), \
             patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}), \
             patch('apps.communications.client_updates.add_zendesk_ticket_tags') as mock_add, \
             patch('apps.communications.client_updates.remove_zendesk_ticket_tags') as mock_remove:
            result = cu.run_due_updates()

        self.assertEqual(result['sent'], 1)
        # Tag written after send
        mock_add.assert_called()
        mock_remove.assert_called()
        update = self.claim.follow_up_updates.get(milestone='DAY_2')
        self.assertEqual(update.state, ClientUpdate.STATE_SENT)
