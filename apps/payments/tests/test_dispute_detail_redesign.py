"""Guard for the dispute-detail redesign (the dispute "single" screen).

The claim-detail rebuild (PR #26) silently dropped display sections because
the guard only checked endpoints loosely. This screen is the operational heart
of the dispute pipeline, so this test pins EVERY workflow endpoint, the two JS
hooks, and the raw-payload debug block — nothing the manager acts with may be
lost in the visual rebuild — plus the new manager-snapshot markers.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from apps.payments.models import Dispute, DisputeDocument

User = get_user_model()


def _dispute(**overrides):
    base = dict(
        paypal_dispute_id='PP-DET-1', status='GATHERING_DATA',
        dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED',
        dispute_amount='120.00', dispute_currency='USD',
        buyer_email='buyer@example.com', buyer_name='Dana Buyer',
        transaction_id='TXN-DET', transaction_date='2026-03-15T10:00:00Z',
        zd_ticket_id='5150',
    )
    base.update(overrides)
    return Dispute.objects.create(**base)


class DisputeDetailRedesignGuard(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='dd_guard', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        # Non-manual + unlinked + deadline soon → every conditional block renders.
        self.dispute = _dispute(
            seller_response_due=timezone.now() + timedelta(days=2))
        self.doc = DisputeDocument.objects.create(
            dispute=self.dispute, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
            status=DisputeDocument.STATUS_DRAFT, version=1)

    def _html(self):
        resp = self.web.get(f'/manager/disputes/{self.dispute.id}/')
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_all_workflow_endpoints_preserved(self):
        html = self._html()
        did = self.dispute.id
        for endpoint in [
            f'/manager/disputes/{did}/refresh-from-paypal/',
            f'/manager/disputes/{did}/prepare-submission/',
            f'/manager/disputes/{did}/submit-to-paypal/',
            f'/manager/disputes/{did}/generate-documents/',
            f'/manager/disputes/{did}/set-category/',
            f'/manager/disputes/{did}/accept-claim/',
            f'/manager/disputes/{did}/link-claim/',            # unlinked → link form
            f'/manager/disputes/documents/{self.doc.id}/edit/',
            f'/manager/disputes/documents/{self.doc.id}/delete/',
        ]:
            self.assertIn(endpoint, html, f'missing endpoint: {endpoint}')

    def test_js_hooks_and_debug_preserved(self):
        html = self._html()
        self.assertIn('composer-notes', html)     # char-counter target
        self.assertIn('composer-count', html)
        self.assertIn('thread-toggle', html)       # conversation expand/collapse
        self.assertIn('Raw PayPal data', html)     # manager debug payload

    def test_snapshot_header_markers(self):
        html = self._html()
        self.assertIn('Dana Buyer', html)          # buyer identity in header
        self.assertIn('USD 120.00', html)          # amount surfaced
        # deadline is the urgent signal — "soon" must be flagged
        self.assertIn('Due', html)
        # back-link to the redesigned list
        self.assertIn('/manager/disputes/', html)
        # broken/decorative status-progress widget removed
        self.assertNotIn('Status Progress', html)
