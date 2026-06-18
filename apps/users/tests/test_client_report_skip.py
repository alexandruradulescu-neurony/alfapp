"""The initial 'what we did' message must be skippable — e.g. the claim reached
LORA late and the client was already updated, so sending a fresh initial would
be wrong. Skip is a reversible toggle and a skipped report refuses to send."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.claims.models import Claim

User = get_user_model()


class InitialReportSkipTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='skip_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='s@example.com', client_name='Skip Me',
            zd_ticket_id='99001', alf_claim_id='ALF99001',
            client_report_draft='Hi there')

    def test_skip_marks_initial_skipped(self):
        self.web.post(reverse('claim_client_report_skip', args=[self.claim.id]))
        self.claim.refresh_from_db()
        self.assertIsNotNone(self.claim.client_report_skipped_at)

    def test_skip_toggles_back(self):
        self.web.post(reverse('claim_client_report_skip', args=[self.claim.id]))
        self.web.post(reverse('claim_client_report_skip', args=[self.claim.id]))
        self.claim.refresh_from_db()
        self.assertIsNone(self.claim.client_report_skipped_at)

    def test_send_refuses_when_skipped(self):
        self.claim.client_report_skipped_at = timezone.now()
        self.claim.save(update_fields=['client_report_skipped_at'])
        self.web.post(reverse('claim_client_report_send', args=[self.claim.id]),
                      {'body': 'should not send'})
        self.claim.refresh_from_db()
        self.assertIsNone(self.claim.client_report_sent_at)
