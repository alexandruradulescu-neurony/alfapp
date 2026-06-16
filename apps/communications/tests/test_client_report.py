"""Client 'what we did' update — template generation, AI-polish fallback, and
the agent review/send actions (2026-06-14)."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from apps.claims.models import Claim
from apps.communications import client_report

User = get_user_model()


class ClientReportTemplateTests(TestCase):
    def _claim(self, **kw):
        base = dict(client_email='lee@example.com', client_name='Lee Foley', alf_claim_id='ALF1',
                    object_description='iPad Tablet\nred case', lost_location='Denver Airport / DEN',
                    flight_data={'number': 'AA3196', 'airline': 'American Airlines'})
        base.update(kw)
        return Claim(**base)

    def test_template_contains_facts(self):
        msg = client_report.build_client_update_template(self._claim())
        self.assertIn('Lee Foley', msg)
        self.assertIn('iPad Tablet', msg)
        self.assertIn('ALF1', msg)
        self.assertIn('American Airlines AA3196', msg)
        self.assertIn('Airport Lost & Found team', msg)

    def test_template_never_promises_recovery(self):
        low = client_report.build_client_update_template(self._claim()).lower()
        for bad in ['we will find', 'guarantee', 'you will get it back', 'will recover', "we'll find"]:
            self.assertNotIn(bad, low)

    def test_message_no_polish_equals_template(self):
        c = self._claim()
        self.assertEqual(client_report.build_client_update_message(c, polish=False),
                         client_report.build_client_update_template(c))

    def test_polish_falls_back_to_template_when_ai_unconfigured(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.ai_api_key = ''
        ss.save()
        c = self._claim()
        self.assertEqual(client_report.build_client_update_message(c, polish=True),
                         client_report.build_client_update_template(c))

    def test_missing_name_uses_safe_fallback(self):
        msg = client_report.build_client_update_template(Claim(client_email='x@example.com'))
        self.assertIn('Dear there,', msg)


class ClientReportActionTests(TestCase):
    def setUp(self):
        self.mgr = User.objects.create_user(username='cr_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.mgr)
        self.claim = Claim.objects.create(
            client_email='lee@example.com', client_name='Lee Foley', zd_ticket_id='97001',
            client_report_draft='Dear Lee, here is what we did...')

    def test_send_posts_public_reply_and_marks_sent(self):
        with patch('apps.integrations.services.post_zendesk_comment', return_value={'id': 1}) as post:
            self.web.post(reverse('claim_client_report_send', args=[self.claim.id]),
                          {'body': 'Hello Lee, an edited update.'})
        self.claim.refresh_from_db()
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], '97001')                    # ticket id
        self.assertIn('edited update', args[1])               # the edited body
        self.assertFalse(kwargs.get('is_internal'))           # PUBLIC reply
        self.assertIsNotNone(self.claim.client_report_sent_at)
        self.assertEqual(self.claim.client_report_draft, 'Hello Lee, an edited update.')

    def test_send_blocked_when_already_sent(self):
        self.claim.client_report_sent_at = timezone.now()
        self.claim.save()
        with patch('apps.integrations.services.post_zendesk_comment') as post:
            self.web.post(reverse('claim_client_report_send', args=[self.claim.id]), {'body': 'x'})
        post.assert_not_called()

    def test_send_requires_nonempty_body(self):
        with patch('apps.integrations.services.post_zendesk_comment') as post:
            self.web.post(reverse('claim_client_report_send', args=[self.claim.id]), {'body': '   '})
        post.assert_not_called()
        self.claim.refresh_from_db()
        self.assertIsNone(self.claim.client_report_sent_at)

    def test_generate_regenerates_draft(self):
        with patch('apps.communications.client_report.build_client_update_message', return_value='NEW DRAFT'):
            self.web.post(reverse('claim_client_report_generate', args=[self.claim.id]))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.client_report_draft, 'NEW DRAFT')


class ClientReportSettingsTests(TestCase):
    def test_trigger_status_field_is_in_settings_form(self):
        from apps.config.forms import SystemSettingsForm
        self.assertIn('client_report_trigger_status', SystemSettingsForm().fields)
