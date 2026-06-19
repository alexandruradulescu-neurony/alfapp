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
