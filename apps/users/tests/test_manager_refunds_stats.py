"""Red-phase test: the manager Refunds page headline stats must be GLOBAL,
not narrowed by the ?status tab filter.

Spec: GET /manager/refunds/ renders 'stats' (total/total_amount/pending/
completed/failed) reflecting ALL refunds regardless of ?status, while only the
paginated list (page_obj) is narrowed by the filter.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.claims.models import Claim
from apps.payments.models import Refund

User = get_user_model()


class ManagerRefundsStatsTests(TestCase):
    URL = '/manager/refunds/'

    def setUp(self):
        self.user = User.objects.create_user(username='refunds_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)

        self.claim = Claim.objects.create(
            alf_claim_id='ALF-REF-STATS', client_email='a@b.com')

        def mk(refund_id, status):
            return Refund.objects.create(
                claim=self.claim,
                paypal_refund_id=refund_id,
                amount=Decimal('10.00'),
                currency='USD',
                status=status,
                refund_type=Refund.TYPE_FULL,
                reason='t',
            )

        # Mixed statuses: 2 COMPLETED, 1 PENDING, 1 FAILED.
        mk('REF-C1', Refund.STATUS_COMPLETED)
        mk('REF-C2', Refund.STATUS_COMPLETED)
        mk('REF-P1', Refund.STATUS_PENDING)
        self.failed = mk('REF-F1', Refund.STATUS_FAILED)

    def test_stats_are_global_but_list_is_filtered(self):
        resp = self.web.get(self.URL + '?status=FAILED')
        self.assertEqual(resp.status_code, 200)

        # Headline stats reflect ALL refunds, ignoring the ?status filter.
        stats = resp.context['stats']
        self.assertEqual(stats['total'], 4)
        self.assertEqual(stats['completed'], 2)
        self.assertEqual(stats['pending'], 1)
        self.assertEqual(stats['failed'], 1)

        # The paginated LIST, however, IS narrowed to the FAILED tab.
        page_ids = {r.id for r in resp.context['page_obj']}
        self.assertEqual(page_ids, {self.failed.id})
