"""Phase 3 — deadline state + won/lost status sync (2026-06-13)."""

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.claims.models import Claim
from apps.payments.models import Dispute
from apps.payments import paypal_disputes_service as svc


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-D-3001', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                status='DOCUMENTS_READY', dispute_life_cycle_stage='CHARGEBACK')
    base.update(kw)
    return Dispute.objects.create(**base)


class CanSubmitEvidenceTests(TestCase):
    def test_inquiry_stage_blocks(self):
        d = _dispute(dispute_life_cycle_stage='INQUIRY')
        self.assertFalse(d.can_submit_evidence)

    def test_chargeback_stage_allows(self):
        d = _dispute(dispute_life_cycle_stage='CHARGEBACK')
        self.assertTrue(d.can_submit_evidence)

    def test_resolved_dispute_blocks(self):
        d = _dispute(status='RESOLVED_WON', dispute_life_cycle_stage='CHARGEBACK')
        self.assertFalse(d.can_submit_evidence)

    def test_unknown_stage_allows(self):
        d = _dispute(dispute_life_cycle_stage='')
        self.assertTrue(d.can_submit_evidence)


class DeadlineStateTests(TestCase):
    def test_overdue(self):
        d = _dispute(seller_response_due=timezone.now() - timedelta(days=1))
        self.assertEqual(d.deadline_state, 'overdue')

    def test_soon(self):
        d = _dispute(seller_response_due=timezone.now() + timedelta(days=2))
        self.assertEqual(d.deadline_state, 'soon')

    def test_ok(self):
        d = _dispute(seller_response_due=timezone.now() + timedelta(days=10))
        self.assertEqual(d.deadline_state, 'ok')

    def test_resolved_has_no_deadline_state(self):
        d = _dispute(status='RESOLVED_WON', seller_response_due=timezone.now() - timedelta(days=1))
        self.assertEqual(d.deadline_state, '')


class SyncDisputeTests(TestCase):
    def test_resolved_seller_favour_marks_won(self):
        _dispute()
        details = {'status': 'RESOLVED', 'dispute_life_cycle_stage': 'CHARGEBACK',
                   'dispute_outcome': {'outcome_code': 'RESOLVED_SELLER_FAVOUR'}}
        with patch.object(svc, 'fetch_dispute_details', return_value=details):
            d = svc.sync_dispute_from_paypal('PP-D-3001')
        self.assertEqual(d.status, 'RESOLVED_WON')

    def test_resolved_buyer_favour_marks_lost(self):
        _dispute()
        details = {'status': 'RESOLVED', 'dispute_outcome': {'outcome_code': 'RESOLVED_BUYER_FAVOUR'}}
        with patch.object(svc, 'fetch_dispute_details', return_value=details):
            d = svc.sync_dispute_from_paypal('PP-D-3001')
        self.assertEqual(d.status, 'RESOLVED_LOST')

    def test_update_refreshes_stage_without_clobbering_workflow_status(self):
        _dispute(status='DOCUMENTS_READY', dispute_life_cycle_stage='INQUIRY')
        details = {'status': 'UNDER_REVIEW', 'dispute_life_cycle_stage': 'CHARGEBACK',
                   'seller_response_due_date': '2026-07-01T10:00:00Z'}
        with patch.object(svc, 'fetch_dispute_details', return_value=details):
            d = svc.sync_dispute_from_paypal('PP-D-3001')
        self.assertEqual(d.status, 'DOCUMENTS_READY')          # workflow status preserved
        self.assertEqual(d.dispute_life_cycle_stage, 'CHARGEBACK')  # stage refreshed
        self.assertIsNotNone(d.seller_response_due)
