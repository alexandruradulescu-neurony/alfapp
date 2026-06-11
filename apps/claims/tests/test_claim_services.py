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
