from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.claims.models import Claim

User = get_user_model()


class ClaimsNavConsistencyTests(TestCase):
    """Sidebar 'Claims', the claim-detail back link, and the post-delete
    redirect must all point at the SAME list, so sidebar → list → claim → back
    is a closed loop (no bouncing between the two vestigial list screens)."""

    def setUp(self):
        self.user = User.objects.create_user(username='nav_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='n@example.com', client_name='Nia Vo',
            zd_ticket_id='98001', alf_claim_id='ALF98001',
            price_paid=Decimal('40.00'))

    def test_sidebar_points_at_canonical_list(self):
        nav = self.web.get(reverse('manager_dashboard')).content.decode()
        self.assertIn(reverse('manager_claims'), nav,
                      'sidebar Claims should point at the canonical list')

    def test_claim_body_links_target_the_canonical_list(self):
        # The body fragment excludes the sidebar, so this isolates the back link
        # and post-delete redirect inside the claim screen itself. Match exact
        # link targets — '/agent/claims/' alone is a prefix of every form-action
        # URL (e.g. /agent/claims/5/client-report/send/), so substring checks
        # would be meaningless here.
        body = self.web.get(reverse('agent_claim_detail_body', args=[self.claim.id])).content.decode()
        self.assertIn(f'href="{reverse("manager_claims")}"', body,
                      'back link should target the canonical list')
        self.assertNotIn(f'href="{reverse("agent_claims")}"', body,
                         'claim-detail back link should not point at the other (vestigial) list')
