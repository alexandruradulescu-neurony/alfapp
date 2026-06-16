"""Tests for the manual 'import claims from Zendesk by ticket id' tool on the
manager Claims Overview page (backlog pull). The heavy lifting is delegated to
import_claim_from_zendesk_ticket (tested separately); these cover the view's
parsing, dedupe, summary, and permission behaviour."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from apps.claims.models import Claim
from apps.config.models import SystemSettings

User = get_user_model()

IMPORT_FN = 'apps.integrations.services.import_claim_from_zendesk_ticket'


def _claim(tid, alf):
    return Claim.objects.create(
        zd_ticket_id=tid, alf_claim_id=alf, client_email=f'{tid}@example.com',
        status='Investigation initiated', status_category='open')


class ManagerClaimsImportTests(TestCase):
    URL = '/manager/claims/import/'

    def setUp(self):
        SystemSettings.get_instance()
        self.manager = User.objects.create_user(
            username='imp_mgr', password='x')
        self.web = Client()
        self.web.force_login(self.manager)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.web.get(self.URL).status_code, 405)

    def test_empty_input_warns_and_imports_nothing(self):
        with patch(IMPORT_FN) as mock_import:
            resp = self.web.post(self.URL, {'ticket_ids': '   '}, follow=True)
        mock_import.assert_not_called()
        self.assertContains(resp, 'No Zendesk ticket IDs')

    def test_parses_mixed_separators_and_urls(self):
        def fake(tid):
            return _claim(tid, f'ALF{tid}'), True
        with patch(IMPORT_FN, side_effect=fake) as mock_import:
            resp = self.web.post(
                self.URL,
                {'ticket_ids': '53973, 54012\n#54110  https://x.zendesk.com/agent/tickets/55000'},
                follow=True)
        called = [c.args[0] for c in mock_import.call_args_list]
        self.assertEqual(called, ['53973', '54012', '54110', '55000'])
        self.assertContains(resp, 'Imported 4')

    def test_deduplicates_repeated_ids(self):
        with patch(IMPORT_FN, side_effect=lambda t: (_claim(t, f'ALF{t}'), True)) as mock_import:
            self.web.post(self.URL, {'ticket_ids': '777 777\n777'}, follow=True)
        self.assertEqual([c.args[0] for c in mock_import.call_args_list], ['777'])

    def test_summarises_imported_existing_and_skipped(self):
        existing = _claim('900', 'ALF900')

        def fake(tid):
            if tid == '900':
                return existing, False           # already in LORA
            if tid == '901':
                return _claim('901', 'ALF901'), True  # freshly imported
            return None, False                   # not a claim ticket / unreachable

        with patch(IMPORT_FN, side_effect=fake):
            resp = self.web.post(self.URL, {'ticket_ids': '900 901 902'}, follow=True)
        self.assertContains(resp, 'Imported 1')
        self.assertContains(resp, '1 already in LORA')
        self.assertContains(resp, '1 skipped')

    def test_one_failure_does_not_abort_the_batch(self):
        def fake(tid):
            if tid == '500':
                raise RuntimeError('boom')
            return _claim(tid, f'ALF{tid}'), True
        with patch(IMPORT_FN, side_effect=fake) as mock_import:
            resp = self.web.post(self.URL, {'ticket_ids': '500 501'}, follow=True)
        self.assertEqual([c.args[0] for c in mock_import.call_args_list], ['500', '501'])
        self.assertContains(resp, 'Imported 1')   # 501 still imported
        self.assertContains(resp, '1 skipped')    # 500 errored

