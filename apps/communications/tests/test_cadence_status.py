"""build_cadence_status: the full client-update cadence with per-milestone
status for the timeline UI. 'Done' counts LORA-sent rows OR the manual Zendesk
macro tag (client_update_N); a passed milestone with nothing is 'missed'."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications import client_updates as cu
from apps.communications.models import ClientUpdate


class CadenceStatusTests(TestCase):
    def _claim(self, age_days=0, **kw):
        claim = Claim.objects.create(
            client_email='c@example.com', zd_ticket_id='1', alf_claim_id='A', **kw)
        Claim.objects.filter(pk=claim.pk).update(
            created_at=timezone.now() - timedelta(days=age_days))
        claim.refresh_from_db()
        return claim

    def _status(self, claim, key):
        for item in cu.build_cadence_status(claim):
            if item['key'] == key:
                return item['status']
        return None

    def test_done_via_zendesk_macro_tag(self):
        # client_update_1 == the DAY_2 milestone's macro tag.
        claim = self._claim(age_days=8, zd_tags=['client_update_1'])
        self.assertEqual(self._status(claim, 'DAY_2'), 'done')

    def test_passed_milestone_with_nothing_is_missed(self):
        claim = self._claim(age_days=8)
        self.assertEqual(self._status(claim, 'DAY_5'), 'missed')     # ~3 days ago
        self.assertEqual(self._status(claim, 'DAY_11'), 'upcoming')  # ~3 days out

    def test_sent_row_done_and_skipped_row_skipped(self):
        claim = self._claim(age_days=0)
        ClientUpdate.objects.create(claim=claim, milestone='DAY_2',
                                    due_at=timezone.now(), state='SENT')
        ClientUpdate.objects.create(claim=claim, milestone='DAY_5',
                                    due_at=timezone.now(), state='SKIPPED')
        self.assertEqual(self._status(claim, 'DAY_2'), 'done')
        self.assertEqual(self._status(claim, 'DAY_5'), 'skipped')

    def test_initial_states(self):
        claim = self._claim(client_report_draft='hi')
        self.assertEqual(self._status(claim, 'initial'), 'drafted')
        claim.client_report_sent_at = timezone.now()
        claim.save(update_fields=['client_report_sent_at'])
        self.assertEqual(self._status(claim, 'initial'), 'done')
