"""Dispute report timestamps must render in the app's display timezone
(America/Chicago, matching Zendesk) — not raw UTC. Regression for the bug where
a 14:00 Zendesk event printed as 19:00 on the PayPal report."""

from datetime import datetime, timezone as dt_tz

from django.test import TestCase

from apps.claims.models import Claim
from apps.payments.document_service import _fmt_zd_time, _to_local, _build_timeline
from apps.payments.models import Dispute


class ReportTimezoneTests(TestCase):
    def test_utc_iso_renders_in_central_summer(self):
        # 19:00 UTC on Jun 19 = 14:00 CDT (UTC-5). This is the exact reported case.
        self.assertEqual(_fmt_zd_time('2026-06-19T19:00:00Z'), 'Jun 19, 2026 14:00')

    def test_utc_iso_renders_in_central_winter_dst(self):
        # 19:00 UTC on Jan 15 = 13:00 CST (UTC-6) — DST handled automatically.
        self.assertEqual(_fmt_zd_time('2026-01-15T19:00:00Z'), 'Jan 15, 2026 13:00')

    def test_aware_datetime_localized(self):
        dt = datetime(2026, 6, 19, 19, 0, tzinfo=dt_tz.utc)
        self.assertEqual(_fmt_zd_time(dt), 'Jun 19, 2026 14:00')
        self.assertEqual(_to_local(dt).hour, 14)

    def test_empty_and_garbage_are_safe(self):
        self.assertEqual(_fmt_zd_time(''), '')
        self.assertEqual(_fmt_zd_time('not-a-date'), 'not-a-date')

    def test_timeline_uses_local_day_and_time(self):
        # 02:00 UTC Jun 20 is still Jun 19 (21:00) in Central — the timeline's
        # first event (claim submitted) must follow the local day AND show a time.
        claim = Claim.objects.create(client_email='b@e.com', alf_claim_id='ALFTZ')
        Claim.objects.filter(pk=claim.pk).update(
            created_at=datetime(2026, 6, 20, 2, 0, tzinfo=dt_tz.utc))
        claim.refresh_from_db()
        d = Dispute.objects.create(
            paypal_dispute_id='PP-TZ', buyer_email='b@e.com', transaction_id='TX',
            transaction_date=datetime(2026, 6, 20, 2, 0, tzinfo=dt_tz.utc),
            dispute_reason='UNAUTHORISED', status='MATCHED', raw_webhook_payload={}, claim=claim)
        labels = {e['label']: e['when'] for e in _build_timeline(d, comments=[])}
        self.assertIn('Claim submitted on our website', labels)
        when = labels['Claim submitted on our website']
        self.assertTrue(when.startswith('Jun 19, 2026'))   # local day, not Jun 20 UTC
        self.assertIn('21:00', when)                       # local time (CDT, UTC-5)
