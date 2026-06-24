"""The dashboard trend chart: a server-rendered SVG line chart over 7/14/30
days, with Orders (claims/day) shown by default and Revenue (income/day) as an
addable overlay line. Both can show at once, each scaled to its own axis."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.claims.models import Claim

User = get_user_model()


class DashboardChartTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username='chart_user', password='x')
        self.web = Client()
        self.web.force_login(self.u)
        # Two claims created "today" → today's bucket: 2 orders, $60 revenue.
        Claim.objects.create(client_email='a@e.com', alf_claim_id='ALFC1', price_paid=Decimal('25.00'))
        Claim.objects.create(client_email='b@e.com', alf_claim_id='ALFC2', price_paid=Decimal('35.00'))

    def test_chart_renders_on_dashboard_orders_only(self):
        resp = self.web.get(reverse('manager_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['chart_range'], 14)
        self.assertTrue(resp.context['chart_show_claims'])
        self.assertFalse(resp.context['chart_show_income'])     # revenue not overlaid by default
        self.assertEqual(len(resp.context['chart_lines']), 1)    # one line: Orders
        self.assertEqual(len(resp.context['chart_lines'][0]['dots']), 14)
        self.assertIn('id="dashboard-chart"', resp.content.decode())
        self.assertIn('<polyline', resp.content.decode())        # it's a line chart

    def test_overlay_both_series(self):
        r = self.web.get(reverse('manager_dashboard_chart') + '?range=7&show=claims,income')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['chart_show_claims'])
        self.assertTrue(r.context['chart_show_income'])
        self.assertEqual(len(r.context['chart_lines']), 2)       # two overlaid lines
        self.assertEqual(len(r.context['chart_lines'][0]['dots']), 7)
        self.assertIsNotNone(r.context['chart_left_axis'])       # Orders axis
        self.assertIsNotNone(r.context['chart_right_axis'])      # Revenue axis
        self.assertIn('2 orders', r.context['chart_subtitle'])
        self.assertIn('$60', r.context['chart_subtitle'])

    def test_revenue_only(self):
        r = self.web.get(reverse('manager_dashboard_chart') + '?range=30&show=income')
        self.assertFalse(r.context['chart_show_claims'])
        self.assertTrue(r.context['chart_show_income'])
        self.assertEqual(len(r.context['chart_lines'][0]['dots']), 30)
        self.assertIsNone(r.context['chart_left_axis'])

    def test_empty_or_bad_show_defaults_to_orders(self):
        for q in ('?show=', '?show=bogus', '?range=999&show=nope'):
            r = self.web.get(reverse('manager_dashboard_chart') + q)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.context['chart_show_claims'])
            self.assertEqual(r.context['chart_range'], 14)

    def test_chart_requires_login(self):
        self.assertEqual(Client().get(reverse('manager_dashboard_chart')).status_code, 302)

    def test_hover_columns_one_per_day_with_value_tooltip(self):
        """Each day gets a hover band + a tooltip listing the date and one line per
        active series, so hovering anywhere in a day's column shows its values."""
        r = self.web.get(reverse('manager_dashboard_chart') + '?range=7&show=claims,income')
        cols = r.context['chart_cols']
        self.assertEqual(len(cols), 7)                       # one band per day
        for c in cols:
            self.assertGreater(c['hw'], 0)                   # the band has width to hover
        today = cols[-1]                                     # setUp put 2 claims / $60 today
        texts = [ln['text'] for ln in today['lines']]
        self.assertEqual(len(texts), 3)                      # date + Orders + Revenue
        self.assertIn('Orders 2', texts)
        self.assertIn('Revenue $60', texts)

    def test_hover_reveal_is_wired_in_fragment(self):
        """The fragment uses a pure-CSS hover reveal (CSP-safe — no JS/eval)."""
        html = self.web.get(reverse('manager_dashboard_chart')).content.decode()
        self.assertIn('group-hover:opacity-100', html)

    def test_claim_buckets_on_submitted_at_not_import_date(self):
        """A claim imported TODAY (created_at=now) but whose WooCommerce order was
        paid 5 days ago must land on the order day, not today — otherwise old imports
        pile onto the import day. submitted_at carries the true claim date."""
        from datetime import timedelta
        from django.utils import timezone
        Claim.objects.create(client_email='old@e.com', alf_claim_id='ALFOLD',
                             price_paid=Decimal('40.00'),
                             submitted_at=timezone.now() - timedelta(days=5))
        cols = self.web.get(reverse('manager_dashboard_chart') + '?range=14&show=claims').context['chart_cols']
        today_texts = [ln['text'] for ln in cols[-1]['lines']]
        self.assertIn('Orders 2', today_texts)              # today still just setUp's 2, not 3
        day5_texts = [ln['text'] for ln in cols[-6]['lines']]   # range 14 → cols[-6] is today-5
        self.assertIn('Orders 1', day5_texts)               # the import landed on its real day
