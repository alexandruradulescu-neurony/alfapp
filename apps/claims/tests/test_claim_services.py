from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.test import TestCase

from apps.claims.services import compute_deadline_at


class ComputeDeadlineAtTests(TestCase):
    def test_no_date_returns_none(self):
        self.assertIsNone(compute_deadline_at(None, '17:00', 'CET'))

    def test_date_only_defaults_to_end_of_day_utc(self):
        result = compute_deadline_at(date(2026, 7, 1), '', '')
        self.assertEqual(result, datetime(2026, 7, 1, 23, 59, 59, tzinfo=ZoneInfo('UTC')))

    def test_24h_time_and_iana_timezone(self):
        result = compute_deadline_at(date(2026, 7, 1), '17:00', 'Europe/Paris')
        self.assertEqual(result, datetime(2026, 7, 1, 17, 0, tzinfo=ZoneInfo('Europe/Paris')))

    def test_12h_time_and_abbreviation(self):
        result = compute_deadline_at(date(2026, 7, 1), '5 PM', 'CET')
        self.assertEqual(result.hour, 17)
        self.assertEqual(str(result.tzinfo), 'Europe/Paris')

    def test_dotted_time(self):
        result = compute_deadline_at(date(2026, 7, 1), '17.30', 'UTC')
        self.assertEqual((result.hour, result.minute), (17, 30))

    def test_garbage_time_and_timezone_fall_back(self):
        result = compute_deadline_at(date(2026, 7, 1), 'soonish', 'Mars/Phobos')
        self.assertEqual((result.hour, result.minute, result.second), (23, 59, 59))
        self.assertEqual(str(result.tzinfo), 'UTC')

    def test_12am_is_midnight(self):
        result = compute_deadline_at(date(2026, 7, 1), '12 AM', 'UTC')
        self.assertEqual(result.hour, 0)

    def test_12pm_is_noon(self):
        result = compute_deadline_at(date(2026, 7, 1), '12 PM', 'UTC')
        self.assertEqual(result.hour, 12)


class LegacyStatusMapTests(TestCase):
    def test_all_legacy_values_map(self):
        from apps.claims.legacy_status_map import map_legacy_status
        expected = {
            'Received': ('Investigation initiated', 'open'),
            'Searching': ('Claim submitted', 'open'),
            'Found': ('Object Found', 'open'),
            'Shipped': ('Object Found', 'open'),
            'Disputed': ('Open', 'open'),
            'REFUND_REQUESTED': ('Refund Requested', 'open'),
            'REFUNDED': ('Closed - Refunded', 'solved'),
            'PARTIALLY_REFUNDED': ('Closed - Refunded', 'solved'),
        }
        for old, new in expected.items():
            self.assertEqual(map_legacy_status(old), new)

    def test_unknown_value_passes_through_with_open_family(self):
        from apps.claims.legacy_status_map import map_legacy_status
        self.assertEqual(map_legacy_status('Investigation initiated'),
                         ('Investigation initiated', 'open'))


class RefreshClaimFromZendeskTests(TestCase):
    """M9: the Zendesk re-extraction merge is now a service, unit-testable
    without HTTP — overwrite vs fill-only, coercion, and never touching status."""

    def _claim(self, **kw):
        from apps.claims.models import Claim
        base = dict(client_email='old@e.com', zd_ticket_id='ZD-RF1',
                    alf_claim_id='ALFRF0001', object_description='',
                    status='Investigation initiated', status_category='open')
        base.update(kw)
        return Claim.objects.create(**base)

    def test_overwrite_replaces_and_fill_only_respects_existing(self):
        from apps.claims.services import refresh_claim_from_zendesk
        claim = self._claim(client_email='old@e.com', object_description='keep-me')
        changed = refresh_claim_from_zendesk(claim, {
            'client_email': 'new@e.com',                 # OVERWRITE -> replaces
            'object_description': 'should-not-overwrite',  # FILL_ONLY, already set -> ignored
            'lost_location': 'Gate 5',                    # OVERWRITE blank -> set
            'price_paid': '42.50',
            'deadline_date': '2026-07-01',
        })
        claim.refresh_from_db()
        self.assertEqual(claim.client_email, 'new@e.com')
        self.assertEqual(claim.object_description, 'keep-me')
        self.assertEqual(claim.lost_location, 'Gate 5')
        self.assertEqual(str(claim.price_paid), '42.50')
        self.assertEqual(str(claim.deadline_date), '2026-07-01')
        self.assertIn('client_email', changed)
        self.assertIn('price_paid', changed)
        self.assertNotIn('object_description', changed)

    def test_fill_only_populates_blank(self):
        from apps.claims.services import refresh_claim_from_zendesk
        claim = self._claim(object_description='')
        changed = refresh_claim_from_zendesk(claim, {'object_description': 'A red bag'})
        claim.refresh_from_db()
        self.assertEqual(claim.object_description, 'A red bag')
        self.assertIn('object_description', changed)

    def test_status_is_never_touched(self):
        from apps.claims.services import refresh_claim_from_zendesk
        claim = self._claim(status='Investigation initiated', status_category='open')
        refresh_claim_from_zendesk(claim, {'client_email': 'x@e.com', 'status': 'SOLVED'})
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'Investigation initiated')
