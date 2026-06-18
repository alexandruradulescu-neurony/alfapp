"""TDD tests for the client-report office-enrichment feature.

Covers:
- build_client_update_template renders a concrete office list when submissions
  are provided, and falls back to the generic line when none are given.
- extract_submissions returns [] gracefully on any failure.
- build_client_update_message passes extracted submissions into the template.
"""

from unittest.mock import patch
from django.test import TestCase
from apps.claims.models import Claim
from apps.communications import client_report
from apps.communications.client_report import build_client_update_template, build_client_update_message


def _claim(**kw):
    base = dict(
        client_email='c@example.com',
        zd_ticket_id='98001',
        alf_claim_id='ALF9800001',
        client_name='Test',
        object_description='grey laptop',
        lost_location='Logan / BOS',
    )
    base.update(kw)
    return Claim.objects.create(**base)


class TemplateRendersSubmissionsTests(TestCase):
    def test_concrete_list_replaces_generic_line(self):
        subs = [
            {'office': 'JetBlue Baggage Services', 'date': 'June 14'},
            {'office': 'TSA BOS', 'date': ''},
        ]
        msg = build_client_update_template(_claim(), submissions=subs)
        self.assertIn('JetBlue Baggage Services', msg)
        self.assertIn('June 14', msg)
        self.assertIn('TSA BOS', msg)
        self.assertNotIn('relevant lost-and-found offices', msg)   # generic line replaced

    def test_empty_submissions_keeps_generic_line(self):
        msg = build_client_update_template(_claim(), submissions=[])
        self.assertIn('relevant lost-and-found offices', msg)

    def test_default_none_keeps_generic_line(self):
        msg = build_client_update_template(_claim())
        self.assertIn('relevant lost-and-found offices', msg)


class ExtractionGracefulTests(TestCase):
    def test_extract_returns_empty_on_failure(self):
        with patch('apps.communications.client_report.fetch_zendesk_comments',
                   side_effect=Exception('boom')):
            self.assertEqual(client_report.extract_submissions(_claim()), [])

    def test_message_uses_extracted_submissions(self):
        with patch.object(client_report, 'extract_submissions',
                          return_value=[{'office': 'TSA BOS', 'date': 'Jun 14'}]):
            msg = build_client_update_message(_claim(), polish=False)
        self.assertIn('TSA BOS', msg)
        self.assertNotIn('relevant lost-and-found offices', msg)

    def test_message_falls_back_to_generic_when_none(self):
        with patch.object(client_report, 'extract_submissions', return_value=[]):
            msg = build_client_update_message(_claim(), polish=False)
        self.assertIn('relevant lost-and-found offices', msg)
