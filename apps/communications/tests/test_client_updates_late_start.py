"""Starting the client-update cadence on a claim that's already days old must
anchor the day-2/5/11/21 schedule to the claim's real submission (created_at)
and skip milestones whose moment has already passed — not restart the clock
from 'now' and queue a misleading 'Day 2' message."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications import client_updates as cu


class LateStartCadenceTests(TestCase):
    def _claim_aged(self, age_days):
        claim = Claim.objects.create(
            client_email='x@example.com', client_name='Aged Claim',
            zd_ticket_id='70001', alf_claim_id='ALF70001')
        # created_at is auto_now_add — override via update() to simulate age.
        Claim.objects.filter(pk=claim.pk).update(
            created_at=timezone.now() - timedelta(days=age_days))
        claim.refresh_from_db()
        return claim

    def test_fresh_claim_starts_at_day_2_in_the_future(self):
        claim = self._claim_aged(0)
        self.assertTrue(cu.start_client_updates(claim))
        fu = claim.follow_up_updates.get()
        self.assertEqual(fu.milestone, 'DAY_2')
        self.assertGreater(fu.due_at, timezone.now())

    def test_old_claim_skips_passed_milestones_and_schedules_next_future(self):
        claim = self._claim_aged(8)
        self.assertTrue(cu.start_client_updates(claim))
        fu = claim.follow_up_updates.get()
        # Day 2 (6 days ago) and Day 5 (3 days ago) are in the past → skipped.
        self.assertNotEqual(fu.milestone, 'DAY_2')
        # The scheduled milestone is still in the future…
        self.assertGreater(fu.due_at, timezone.now())
        # …and is anchored to the claim's real created_at, not to 'now'.
        offset = cu._offset_for(fu.milestone)
        self.assertIsNotNone(offset)
        expected = claim.created_at + timedelta(days=offset)
        self.assertAlmostEqual(fu.due_at, expected, delta=timedelta(seconds=5))
