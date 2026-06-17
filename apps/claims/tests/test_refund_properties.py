"""TDD (Red phase) tests for the Claim model's refund-summary properties.

These tests are written from the spec ONLY, blind to the implementation in
apps/claims/models.py. They pin down the DESIRED behavior of four properties:

    claim.has_refund      -> bool
    claim.refund_total    -> Decimal  (sum of COMPLETED refunds; 0.00 if none)
    claim.latest_refund   -> Refund | None  (most-recent created_at)
    claim.refund_status   -> str | None     (status of latest_refund)

The headline requirement is prefetch-friendliness: once a claim has its
`refunds` prefetched, reading all four properties must issue ZERO extra
queries. On the current implementation this is expected to FAIL (it re-queries
despite the prefetch) — that failure is the Red signal we want.
"""

import itertools
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.claims.models import Claim
from apps.payments.models import Refund


_seq = itertools.count(1)


def _make_claim():
    """A bare claim with a unique alf_claim_id (column is varchar(20))."""
    return Claim.objects.create(
        alf_claim_id=f"ALF-RP-{next(_seq)}",
        client_email="a@b.com",
    )


def _add_refund(claim, *, amount, status):
    """Attach a refund to a claim. paypal_refund_id is unique per refund."""
    return Refund.objects.create(
        claim=claim,
        paypal_refund_id=f"PPR-RP-{next(_seq)}",
        amount=Decimal(amount),
        currency="USD",
        status=status,
        refund_type=Refund.TYPE_FULL,
        reason="t",
    )


class RefundPropertyValueTests(TestCase):
    """Value correctness — kept separate from the query-count test so the
    prefetch optimization can't quietly break the answers."""

    def test_has_refund_false_with_no_refunds(self):
        claim = _make_claim()
        self.assertFalse(claim.has_refund)

    def test_has_refund_true_with_one_refund(self):
        claim = _make_claim()
        _add_refund(claim, amount="25.00",
                    status=Refund.STATUS_COMPLETED)
        # reload to drop any per-instance cache from creation
        claim = Claim.objects.get(pk=claim.pk)
        self.assertTrue(claim.has_refund)

    def test_refund_total_sums_only_completed(self):
        """A PENDING refund must NOT count toward the total."""
        claim = _make_claim()
        _add_refund(claim, amount="25.00",
                    status=Refund.STATUS_COMPLETED)
        _add_refund(claim, amount="10.50",
                    status=Refund.STATUS_COMPLETED)
        _add_refund(claim, amount="99.00",
                    status=Refund.STATUS_PENDING)
        claim = Claim.objects.get(pk=claim.pk)
        self.assertEqual(claim.refund_total, Decimal("35.50"))

    def test_refund_total_zero_when_none_completed(self):
        """Refunds exist but none are COMPLETED -> Decimal('0.00')."""
        claim = _make_claim()
        _add_refund(claim, amount="40.00",
                    status=Refund.STATUS_PENDING)
        claim = Claim.objects.get(pk=claim.pk)
        self.assertEqual(claim.refund_total, Decimal("0.00"))

    def test_refund_total_zero_with_no_refunds(self):
        claim = _make_claim()
        self.assertEqual(claim.refund_total, Decimal("0.00"))

    def test_latest_refund_is_most_recent_created_at(self):
        claim = _make_claim()
        older = _add_refund(claim, amount="5.00",
                            status=Refund.STATUS_COMPLETED)
        newer = _add_refund(claim, amount="7.00",
                            status=Refund.STATUS_PENDING)
        # created_at is auto-set; pin explicit, well-separated timestamps so the
        # ordering is unambiguous regardless of insert order / clock resolution.
        now = timezone.now()
        Refund.objects.filter(pk=older.pk).update(
            created_at=now - timedelta(hours=2))
        Refund.objects.filter(pk=newer.pk).update(
            created_at=now - timedelta(hours=1))

        claim = Claim.objects.get(pk=claim.pk)
        self.assertEqual(claim.latest_refund.pk, newer.pk)

    def test_latest_refund_none_with_no_refunds(self):
        claim = _make_claim()
        self.assertIsNone(claim.latest_refund)

    def test_refund_status_matches_latest_refund(self):
        claim = _make_claim()
        older = _add_refund(claim, amount="5.00",
                            status=Refund.STATUS_COMPLETED)
        newer = _add_refund(claim, amount="7.00",
                            status=Refund.STATUS_PENDING)
        now = timezone.now()
        Refund.objects.filter(pk=older.pk).update(
            created_at=now - timedelta(hours=2))
        Refund.objects.filter(pk=newer.pk).update(
            created_at=now - timedelta(hours=1))

        claim = Claim.objects.get(pk=claim.pk)
        self.assertEqual(claim.refund_status, Refund.STATUS_PENDING)

    def test_refund_status_none_with_no_refunds(self):
        claim = _make_claim()
        self.assertIsNone(claim.refund_status)


class RefundPropertyPrefetchTests(TestCase):
    """The key requirement: with refunds prefetched, reading all four
    properties must hit the DB ZERO additional times."""

    def test_all_four_properties_zero_queries_when_prefetched(self):
        claim = _make_claim()
        _add_refund(claim, amount="25.00",
                    status=Refund.STATUS_COMPLETED)
        _add_refund(claim, amount="10.00",
                    status=Refund.STATUS_PENDING)

        # Force-evaluate the prefetch BEFORE the assertion block: .get() runs the
        # claim query and prefetch_related triggers the refunds query here, so
        # those queries are not counted below.
        claim = Claim.objects.prefetch_related("refunds").get(pk=claim.pk)

        with self.assertNumQueries(0):
            _ = claim.has_refund
            _ = claim.refund_total
            _ = claim.latest_refund
            _ = claim.refund_status
