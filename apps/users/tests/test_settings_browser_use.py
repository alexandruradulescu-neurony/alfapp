import pytest
from django.test import Client
from django.contrib.auth import get_user_model
from django.urls import reverse
from apps.config.models import SystemSettings

User = get_user_model()


@pytest.mark.django_db
class TestBrowserUseSettings:
    def _client(self):
        User.objects.create_user(username='bu_settings', password='x')
        c = Client(); c.login(username='bu_settings', password='x'); return c

    def test_defaults(self):
        ss = SystemSettings.get_instance()
        assert ss.form_filling_enabled is False
        assert ss.browser_use_model == 'claude-sonnet-4.6'
        assert ss.browser_use_api_key == ''

    def test_save_flag_and_model_and_key(self):
        c = self._client()
        ss = SystemSettings.get_instance()
        from apps.config.forms import SystemSettingsForm
        data = {f: (getattr(ss, f) or '') for f in SystemSettingsForm.Meta.fields}
        data.update({'form_filling_enabled': 'on',
                     'browser_use_model': 'claude-sonnet-4.6',
                     'browser_use_api_key': 'bu_secret_123'})
        resp = c.post(reverse('manager_settings'), data)
        assert resp.status_code in (200, 302)
        ss.refresh_from_db()
        assert ss.form_filling_enabled is True
        assert ss.browser_use_model == 'claude-sonnet-4.6'
        assert ss.browser_use_api_key == 'bu_secret_123'

    def test_blank_key_preserves_existing(self):
        ss = SystemSettings.get_instance()
        ss.browser_use_api_key = 'bu_keep_me'; ss.save()
        c = self._client()
        from apps.config.forms import SystemSettingsForm
        data = {f: (getattr(ss, f) or '') for f in SystemSettingsForm.Meta.fields}
        data['browser_use_api_key'] = ''  # blank -> must NOT wipe
        c.post(reverse('manager_settings'), data)
        ss.refresh_from_db()
        assert ss.browser_use_api_key == 'bu_keep_me'
