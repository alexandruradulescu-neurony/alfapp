"""The dashboard trend chart: a 7/14/30-day series toggleable between
claims/day and income/day, rendered server-side and HTMX-swapped."""

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
        # Two claims created "today" → today's bucket: 2 claims, $60 in fees.
        Claim.objects.create(client_email='a@e.com', alf_claim_id='ALFC1', price_paid=Decimal('25.00'))
        Claim.objects.create(client_email='b@e.com', alf_claim_id='ALFC2', price_paid=Decimal('35.00'))

    def test_chart_renders_on_dashboard(self):
        resp = self.web.get(reverse('manager_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['chart_range'], 14)        # default range
        self.assertEqual(resp.context['chart_metric'], 'claims')  # default metric
        self.assertEqual(len(resp.context['chart_bars']), 14)
        self.assertIn('id="dashboard-chart"', resp.content.decode())

    def test_range_and_metric_toggles(self):
        r7 = self.web.get(reverse('manager_dashboard_chart') + '?range=7&metric=claims')
        self.assertEqual(r7.status_code, 200)
        self.assertEqual(len(r7.context['chart_bars']), 7)
        self.assertEqual(r7.context['chart_bars'][-1]['value'], 2)   # today = the 2 claims
        self.assertIn('2 claims', r7.context['chart_total_label'])

        ri = self.web.get(reverse('manager_dashboard_chart') + '?range=30&metric=income')
        self.assertEqual(ri.context['chart_metric'], 'income')
        self.assertEqual(len(ri.context['chart_bars']), 30)
        self.assertEqual(ri.context['chart_bars'][-1]['value'], 60.0)  # $25 + $35
        self.assertIn('$60', ri.context['chart_total_label'])

    def test_bad_params_fall_back_to_defaults(self):
        r = self.web.get(reverse('manager_dashboard_chart') + '?range=999&metric=bogus')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['chart_range'], 14)
        self.assertEqual(r.context['chart_metric'], 'claims')

    def test_chart_requires_login(self):
        resp = Client().get(reverse('manager_dashboard_chart'))
        self.assertEqual(resp.status_code, 302)
