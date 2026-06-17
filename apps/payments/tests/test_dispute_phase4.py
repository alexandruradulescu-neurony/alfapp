"""Phase 4 — reason enum fix, category dropdown, stage gating (2026-06-13)."""

from datetime import datetime, timezone as dt_tz

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.payments.models import Dispute

User = get_user_model()


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-D-4001', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                status='DOCUMENTS_READY', dispute_life_cycle_stage='CHARGEBACK')
    base.update(kw)
    return Dispute.objects.create(**base)


class ReasonEnumTests(TestCase):
    def test_uses_paypal_british_unauthorised(self):
        codes = dict(Dispute.REASON_CHOICES)
        self.assertIn('UNAUTHORISED', codes)
        self.assertNotIn('UNAUTHORIZED_TRANSACTION', codes)
        # the newer PayPal reasons are present
        self.assertIn('PAYMENT_BY_OTHER_MEANS', codes)
        self.assertIn('CANCELED_RECURRING_BILLING', codes)


class SetCategoryTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='disp_mgr', password='x')
        self.agent = User.objects.create_user(username='disp_agent', password='x')
        self.web = Client()
        self.web.force_login(self.manager)
        self.dispute = _dispute()
        self.url = f'/manager/disputes/{self.dispute.id}/set-category/'

    def test_manager_sets_category(self):
        resp = self.web.post(self.url, {'category': 'UNAUTHORISED'})
        self.assertEqual(resp.status_code, 302)
        self.dispute.refresh_from_db()
        self.assertEqual(self.dispute.dispute_reason, 'UNAUTHORISED')

    def test_unknown_category_rejected(self):
        resp = self.web.post(self.url, {'category': 'BOGUS'})
        self.assertEqual(resp.status_code, 302)
        self.dispute.refresh_from_db()
        self.assertNotEqual(self.dispute.dispute_reason, 'BOGUS')


class StageGatingActionTests(TestCase):
    def setUp(self):
        self.manager = User.objects.create_user(username='gate_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.manager)

    def test_evidence_blocked_at_inquiry_stage(self):
        """At the INQUIRY stage PayPal is message-only, so no submission window
        opens (submit_endpoint == '') and the composer can't send — the gate that
        used to live in the removed send-evidence view, now via submit_endpoint."""
        from apps.payments.models import DisputeSubmission
        d = _dispute(paypal_dispute_id='PP-D-4002', dispute_life_cycle_stage='INQUIRY')
        self.assertEqual(d.submit_endpoint, '')  # no channel offered at inquiry
        DisputeSubmission.objects.create(dispute=d, notes='ready', status='DRAFT')
        resp = self.web.post(f'/manager/disputes/{d.id}/submit-to-paypal/', follow=True)
        self.assertEqual(resp.status_code, 200)
        # blocked before reaching PayPal; status unchanged
        d.refresh_from_db()
        self.assertEqual(d.status, 'DOCUMENTS_READY')
        # Verify the VIEW's own no-endpoint flash (not the template's standing banner).
        msgs = [str(m) for m in resp.context['messages']]
        self.assertTrue(any('accepting a submission' in m for m in msgs),
                        f"expected the no-endpoint flash message; got {msgs}")
