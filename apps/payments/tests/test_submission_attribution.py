"""Red-phase test: _record_submission_outcome must not erase an existing
submitter when called with performed_by=None.

The outcome recorder persists a DisputeSubmission's terminal state. When a
follow-up call (e.g. a re-sync that doesn't know who acted) passes
performed_by=None, it must NOT overwrite a previously-recorded human
attribution (submitted_by) with NULL.
"""

from datetime import datetime, timezone as dt_tz

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.payments import paypal_disputes_service as pds
from apps.payments.models import Dispute, DisputeSubmission

User = get_user_model()


class RecordSubmissionAttributionTests(TestCase):
    def test_none_performer_does_not_erase_existing_submitter(self):
        user = User.objects.create_user(username='submitter1', password='x')
        dispute = Dispute.objects.create(
            paypal_dispute_id='PP-ATTR', buyer_email='b@example.com',
            transaction_id='TX',
            transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
            dispute_reason='UNAUTHORISED', status='MATCHED',
            raw_webhook_payload={})
        sub = DisputeSubmission.objects.create(
            dispute=dispute, status=DisputeSubmission.STATUS_DRAFT,
            submitted_by=user)

        pds._record_submission_outcome(
            sub, status=DisputeSubmission.STATUS_SUBMITTED,
            performed_by=None, response={'ok': 1})

        sub.refresh_from_db()
        self.assertEqual(
            sub.submitted_by_id, user.id,
            'performed_by=None must not erase the existing human attribution')
