"""The redesigned claims list segments by tab/lens (Problems · Object found ·
Refunds · Disputes · Open · Solved · All) with live per-tab counts. Lenses are
non-exclusive views, not folders."""

from datetime import datetime, timezone as dt_tz
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.payments.models import Refund, Dispute

User = get_user_model()


class ManagerClaimsTabsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='tabs_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)

        def mk(alf, status='Investigation initiated', cat='open', **kw):
            return Claim.objects.create(client_email=f'{alf}@e.com', alf_claim_id=alf,
                                        status=status, status_category=cat, **kw)

        self.plain = mk('PLAIN')
        self.risky = mk('RISKY', risk_level='at_risk')
        self.emailed = mk('EMAILED')
        EmailLog.objects.create(claim=self.emailed, subject='s', body='b',
                                action_required=True, auto_resolved=False)
        self.found = mk('FOUND', status='Object Found')
        self.solved = mk('SOLVED', status='Solved', cat='solved')
        self.refunded = mk('REFUND')
        Refund.objects.create(claim=self.refunded, paypal_refund_id='PP-T1',
                              amount=Decimal('10.00'), refund_type='FULL', reason='t')
        self.disputed = mk('DISPUTE')
        Dispute.objects.create(paypal_dispute_id='PP-D1', claim=self.disputed,
                               zd_ticket_id='1', buyer_email='d@e.com', transaction_id='TX',
                               transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                               dispute_reason='UNAUTHORISED')

    def _ids(self, tab):
        resp = self.web.get(reverse('manager_claims') + f'?tab={tab}')
        self.assertEqual(resp.status_code, 200)
        return {c.id for c in resp.context['claims']}

    def test_problems_is_at_risk_or_emails_awaiting_reply(self):
        ids = self._ids('problems')
        self.assertIn(self.risky.id, ids)
        self.assertIn(self.emailed.id, ids)
        self.assertNotIn(self.plain.id, ids)
        self.assertNotIn(self.solved.id, ids)

    def test_object_found_tab(self):
        ids = self._ids('object_found')
        self.assertIn(self.found.id, ids)
        self.assertNotIn(self.plain.id, ids)

    def test_refunds_tab(self):
        ids = self._ids('refunds')
        self.assertIn(self.refunded.id, ids)
        self.assertNotIn(self.plain.id, ids)

    def test_disputes_tab(self):
        ids = self._ids('disputes')
        self.assertIn(self.disputed.id, ids)
        self.assertNotIn(self.plain.id, ids)

    def test_open_excludes_solved_and_solved_tab(self):
        self.assertIn(self.plain.id, self._ids('open'))
        self.assertNotIn(self.solved.id, self._ids('open'))
        self.assertIn(self.solved.id, self._ids('solved'))
        self.assertNotIn(self.plain.id, self._ids('solved'))

    def test_default_tab_is_problems(self):
        resp = self.web.get(reverse('manager_claims'))
        self.assertEqual(resp.context['tab'], 'problems')

    def test_tab_counts_present(self):
        resp = self.web.get(reverse('manager_claims'))
        counts = resp.context['tab_counts']
        self.assertEqual(counts['solved'], 1)
        self.assertEqual(counts['refunds'], 1)
        self.assertEqual(counts['disputes'], 1)
        self.assertGreaterEqual(counts['problems'], 2)

    def test_redesign_markers_present_and_old_removed(self):
        html = self.web.get(reverse('manager_claims') + '?tab=all').content.decode()
        # New: tabs + rows clickable to detail
        self.assertIn('?tab=problems', html)
        self.assertIn('Object found', html)
        self.assertIn('window.location=', html)
        # Removed: deadline column, PDF icon + its info box, the stat cards
        self.assertNotIn('Deadline', html)
        self.assertNotIn('bi-file-earmark-pdf', html)
        self.assertNotIn('All claims ever', html)
