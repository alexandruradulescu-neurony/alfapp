"""get_woocommerce_order_date: the order's payment moment (date_paid_gmt, falling
back to date_created_gmt) as tz-aware UTC — the true claim date source."""
import json
from unittest.mock import patch, MagicMock

from django.test import TestCase

from apps.payments import woocommerce_service as wc


def _resp(body):
    m = MagicMock()
    m.read.return_value = json.dumps(body).encode('utf-8')
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


@patch('apps.payments.woocommerce_service._wc_credentials',
       return_value=('https://store.test', 'k', 's'))
class WooCommerceOrderDateTests(TestCase):
    def test_uses_paid_date_and_returns_utc_aware(self, _creds):
        with patch('urllib.request.urlopen',
                   return_value=_resp({'date_paid_gmt': '2026-02-03T21:14:00',
                                       'date_created_gmt': '2026-02-01T10:00:00'})):
            dt = wc.get_woocommerce_order_date('123')
        self.assertIsNotNone(dt)
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour, dt.minute), (2026, 2, 3, 21, 14))
        self.assertIsNotNone(dt.tzinfo)                     # tz-aware (UTC)

    def test_falls_back_to_created_when_not_yet_paid(self, _creds):
        with patch('urllib.request.urlopen',
                   return_value=_resp({'date_paid_gmt': None,
                                       'date_created_gmt': '2026-02-01T10:00:00'})):
            dt = wc.get_woocommerce_order_date('123')
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 2, 1))

    def test_none_on_fetch_error(self, _creds):
        with patch('urllib.request.urlopen', side_effect=Exception('network')):
            self.assertIsNone(wc.get_woocommerce_order_date('123'))

    def test_none_when_order_has_no_dates(self, _creds):
        with patch('urllib.request.urlopen', return_value=_resp({'id': 1})):
            self.assertIsNone(wc.get_woocommerce_order_date('123'))
