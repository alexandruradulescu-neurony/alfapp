"""Smoke test: every manager-facing page renders (HTTP 200) without template
errors. A safety net for the design-refresh template sweep — most of these
pages have no other test coverage, so a broken {% url %}, tag, or syntax error
would otherwise ship silently."""

from datetime import datetime, timezone as dt_tz
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from apps.claims.models import Claim
from apps.payments.models import Dispute

User = get_user_model()


class ManagerPageSmokeTests(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='smoke_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)
        self.claim = Claim.objects.create(
            client_email='lee@example.com', client_name='Lee Foley', alf_claim_id='ALF1',
            zd_ticket_id='97001', price_paid=Decimal('74.00'))

    def _get_ok(self, url_name, *args):
        resp = self.web.get(reverse(url_name, args=args))
        self.assertEqual(resp.status_code, 200, f"{url_name} returned {resp.status_code}")

    def test_nav_pages_render(self):
        for name in ['manager_dashboard', 'manager_claims', 'agent_claims', 'agent_emails',
                     'disputes:dispute_list', 'manager_refunds', 'manager_settings',
                     'manager_users']:
            self._get_ok(name)

    def test_claim_detail_renders(self):
        self._get_ok('agent_claim_detail', self.claim.id)

    def test_secondary_pages_render(self):
        # chat + test_ai render under the main shell as a manager
        self._get_ok('agent:agent-chat')
        self._get_ok('test_ai')
        # login renders for an unauthenticated visitor (its own auth shell)
        anon = Client()
        resp = anon.get(reverse('login'))
        self.assertEqual(resp.status_code, 200, f"login returned {resp.status_code}")

    def test_dispute_detail_renders(self):
        dispute = Dispute.objects.create(
            paypal_dispute_id='PP-SMOKE-1', claim=self.claim, zd_ticket_id='97001',
            buyer_email='lee@example.com', transaction_id='TX',
            transaction_date=datetime(2026, 6, 1, tzinfo=dt_tz.utc), dispute_reason='UNAUTHORISED')
        # dispute_detail may fetch Zendesk; tolerate that by allowing it to no-op.
        from unittest.mock import patch
        with patch('apps.integrations.services.fetch_zendesk_ticket_full', return_value=None), \
             patch('apps.integrations.services.fetch_zendesk_comments', return_value=[]):
            self._get_ok('disputes:dispute_detail', dispute.id)


class ClaimDetailControlsPreservedTests(TestCase):
    """The redesign is presentation-only — every action the screen drove must
    still be reachable from the rendered HTML."""

    def setUp(self):
        self.user = User.objects.create_user(username='ctrl_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='c@example.com', client_name='Cy Ng',
            zd_ticket_id='96001', alf_claim_id='ALF96001',
            price_paid=Decimal('60.00'))

    def test_action_endpoints_present_in_rendered_screen(self):
        # Fresh ticketed claim, no cadence started → these controls are all
        # applicable and must be wired into the markup (not built in JS).
        resp = self.web.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # Same-app form action: "start client updates" shows when no cadence yet.
        self.assertIn(reverse('client_updates_start', args=[self.claim.id]), html,
                      'client_updates_start URL missing from rebuilt screen')
        # Cross-app JSON endpoints (literal paths now in markup as hx-post).
        self.assertIn(f'/api/claims/{self.claim.id}/update-from-zendesk/', html)
        self.assertIn(f'/api/claims/{self.claim.id}/check-email/', html)

    def test_no_inline_script_functions_remain(self):
        resp = self.web.get(reverse('agent_claim_detail', args=[self.claim.id]))
        html = resp.content.decode()
        # The 240-line inline <script> is replaced by lora-htmx.js + Alpine attrs.
        self.assertNotIn('function updateFromZendesk', html)
        self.assertNotIn('function checkEmail', html)

    def test_no_eval_dependent_attributes_csp_safe(self):
        # Production CSP forbids unsafe-eval. Alpine directives (x-data/x-show/
        # @click ...) and htmx hx-on both turn strings into code at runtime, so
        # they silently die under the CSP. Only CSP-safe patterns (native
        # <details>/<dialog>, inline onclick, external JS) are allowed here.
        resp = self.web.get(reverse('agent_claim_detail', args=[self.claim.id]))
        html = resp.content.decode()
        for attr in ['x-data', 'x-show', 'x-cloak', 'x-init', 'hx-on', '@click', 'x-on:']:
            self.assertNotIn(attr, html, f'{attr} needs unsafe-eval — blocked by the production CSP')

    def test_no_leaked_template_syntax(self):
        # A multi-line {# #} comment once leaked onto the page (Django's short
        # comment only works on one line). Nothing template-y should survive
        # into the rendered HTML.
        resp = self.web.get(reverse('agent_claim_detail', args=[self.claim.id]))
        html = resp.content.decode()
        self.assertNotIn('{#', html, 'a template comment leaked into the page')
        self.assertNotIn('{%', html, 'a template tag leaked into the page')

    def test_destructive_actions_visible_to_signed_in_user(self):
        # The manager/agent role split was removed — there is one trusted user,
        # so delete / mark-as-disputed / grant-refund must be reachable (no dead
        # `user.role == 'MANAGER'` gate hiding them from everyone).
        resp = self.web.get(reverse('agent_claim_detail', args=[self.claim.id]))
        html = resp.content.decode()
        self.assertIn(f'/api/claims/claims/{self.claim.id}/', html, 'delete action hidden')
        self.assertIn('/api/payments/refunds/issue/', html, 'grant refund hidden')
        self.assertIn(reverse('disputes:dispute_create'), html, 'mark-as-disputed hidden')
