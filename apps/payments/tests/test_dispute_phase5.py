"""Phase 5 framework — evidence bundle + category→template registry (2026-06-13).

Per-category report LAYOUTS wait on the user's report model; the bundle
assembler and the registry (the report-independent core) are built now.
"""

from datetime import datetime, timezone as dt_tz
from unittest.mock import patch

from django.test import TestCase

from apps.claims.models import Claim
from apps.payments.models import Dispute
from apps.payments import document_service as ds


def _dispute(**kw):
    base = dict(paypal_dispute_id='PP-D-5001', buyer_email='b@example.com',
                transaction_id='TX', transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc),
                dispute_reason='MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
    base.update(kw)
    return Dispute.objects.create(**base)


class ReportTemplateRegistryTests(TestCase):
    def test_defaults_to_generic(self):
        d = _dispute(dispute_reason='UNAUTHORISED')
        self.assertEqual(ds.report_template_for(d), ds.GENERIC_EVIDENCE_TEMPLATE)

    def test_registered_category_uses_its_template(self):
        d = _dispute(dispute_reason='UNAUTHORISED')
        with patch.dict(ds.CATEGORY_REPORT_TEMPLATES,
                        {'UNAUTHORISED': 'disputes/unauthorised_report.html'}):
            self.assertEqual(ds.report_template_for(d), 'disputes/unauthorised_report.html')


class EvidenceBundleTests(TestCase):
    def test_bundle_has_all_sections(self):
        claim = Claim.objects.create(client_email='b@example.com', zd_ticket_id='97001')
        d = _dispute(claim=claim, zd_ticket_id='97001')
        with patch.object(ds, '_fetch_zendesk_ticket_full',
                          return_value={'ticket': {'id': '97001'}, 'comments': [{'body': 'hi'}]}):
            bundle = ds.build_dispute_evidence_bundle(d)
        self.assertEqual(bundle['dispute'], d)
        self.assertEqual(bundle['ticket'], {'id': '97001'})
        self.assertEqual(bundle['comments'], [{'body': 'hi'}])
        self.assertEqual(bundle['category'], 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED')
        self.assertEqual(bundle['category_label'], 'Item/Service Not Received')
        for key in ('screenshots', 'claim_evidence', 'communication_history', 'generated_at'):
            self.assertIn(key, bundle)
