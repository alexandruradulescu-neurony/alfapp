"""Claim.submitted_at = the TRUE claim date (WooCommerce order payment date), so
reports + the update cadence stop treating the LORA import day as the claim day."""
from datetime import datetime, timedelta, timezone as dt_utc
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.claims.models import Claim
from apps.claims.services import backfill_claim_dates


class ClaimDatePropertyTests(TestCase):
    def test_claim_date_falls_back_to_created_at_when_unset(self):
        c = Claim.objects.create(client_email='a@e.com', alf_claim_id='ALFCD1')
        self.assertEqual(c.claim_date, c.created_at)        # no submitted_at → created_at

    def test_claim_date_prefers_submitted_at(self):
        when = timezone.now() - timedelta(days=10)
        c = Claim.objects.create(client_email='a@e.com', alf_claim_id='ALFCD2', submitted_at=when)
        self.assertEqual(c.claim_date, when)                # the true claim date wins


class BackfillClaimDatesTests(TestCase):
    def test_sets_submitted_at_from_woocommerce_order_date(self):
        c = Claim.objects.create(client_email='a@e.com', alf_claim_id='ALFB1', woocommerce_id='555')
        paid = datetime(2026, 2, 3, 21, 14, tzinfo=dt_utc.utc)
        with patch('apps.payments.woocommerce_service.get_woocommerce_order_date', return_value=paid):
            result = backfill_claim_dates(only_missing=True, limit=1)  # newest first → just this one
        c.refresh_from_db()
        self.assertEqual(c.submitted_at, paid)
        self.assertEqual(result['updated'], 1)

    def test_no_order_claim_is_left_for_created_at_fallback(self):
        c = Claim.objects.create(client_email='a@e.com', alf_claim_id='ALFB2', woocommerce_id='')
        with patch('apps.payments.woocommerce_service.get_woocommerce_order_date') as wc:
            backfill_claim_dates(only_missing=True)
        wc.assert_not_called()                              # no order → no lookup
        c.refresh_from_db()
        self.assertIsNone(c.submitted_at)                   # stays null → reports use created_at


class SubmissionAnchorTests(TestCase):
    def test_cadence_anchor_uses_submitted_at_over_import_date(self):
        # An old claim imported today: the cadence must count from when it was
        # actually submitted (submitted_at), not from the import-time fallback.
        from apps.communications import client_updates as cu
        when = timezone.now() - timedelta(days=3)
        c = Claim.objects.create(client_email='a@e.com', alf_claim_id='ALFSA1', submitted_at=when)
        anchor = cu._submission_anchor(c, fallback=timezone.now())  # no cadence rows yet
        self.assertEqual(anchor, when)
