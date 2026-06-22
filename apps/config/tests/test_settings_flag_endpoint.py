"""Instant automation-switch endpoint: POST /api/services/settings-flag/."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse

from apps.config.models import SystemSettings

User = get_user_model()


class ToggleSettingFlagTests(TestCase):
    def setUp(self):
        self.url = reverse('services:settings-flag')
        self.mgr = User.objects.create_user(username='flag_mgr', password='x')
        self.agent = User.objects.create_user(username='flag_agent', password='x')
        self.web = Client()

    def _post(self, body):
        return self.web.post(self.url, data=json.dumps(body), content_type='application/json')

    def test_manager_can_flip_flag_both_ways(self):
        self.web.force_login(self.mgr)
        self.assertEqual(self._post({'flag': 'email_sweep_autorun', 'enabled': True}).status_code, 200)
        self.assertTrue(SystemSettings.get_instance().email_sweep_autorun)
        self._post({'flag': 'email_sweep_autorun', 'enabled': False})
        self.assertFalse(SystemSettings.get_instance().email_sweep_autorun)

    def test_client_updates_autosend_is_toggleable(self):
        self.web.force_login(self.mgr)
        self._post({'flag': 'client_updates_autosend', 'enabled': True})
        self.assertTrue(SystemSettings.get_instance().client_updates_autosend)

    def test_unknown_flag_rejected(self):
        self.web.force_login(self.mgr)
        resp = self._post({'flag': 'is_superuser', 'enabled': True})
        self.assertEqual(resp.status_code, 400)

    def test_string_false_is_treated_as_false(self):
        # "false" submitted as TEXT must DISABLE the flag (bare bool() made it truthy).
        self.web.force_login(self.mgr)
        self._post({'flag': 'email_sweep_autorun', 'enabled': True})
        self.assertTrue(SystemSettings.get_instance().email_sweep_autorun)
        self._post({'flag': 'email_sweep_autorun', 'enabled': 'false'})
        self.assertFalse(SystemSettings.get_instance().email_sweep_autorun)

    def test_recover_orphan_emails_flag_is_toggleable(self):
        self.web.force_login(self.mgr)
        self.assertEqual(self._post({'flag': 'recover_orphan_emails', 'enabled': True}).status_code, 200)
        self.assertTrue(SystemSettings.get_instance().recover_orphan_emails)
        self._post({'flag': 'recover_orphan_emails', 'enabled': False})
        self.assertFalse(SystemSettings.get_instance().recover_orphan_emails)
