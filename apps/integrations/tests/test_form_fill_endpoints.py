import pytest
from unittest.mock import patch
from django.urls import reverse
from rest_framework.test import APIClient
from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.integrations.models import FormFill

SECRET = 'sidebar-secret-xyz'


@pytest.fixture
def settings_obj(db):
    ss = SystemSettings.get_instance()
    ss.sidebar_secret_token = SECRET
    ss.browser_use_api_key = 'bu_test'
    ss.form_filling_enabled = True
    ss.save()
    return ss


@pytest.fixture
def api():
    return APIClient()


def _auth(**extra):
    return {'HTTP_AUTHORIZATION': f'Bearer {SECRET}', **extra}


@pytest.mark.django_db
def test_start_requires_auth(api, settings_obj):
    resp = api.post(reverse('zd-form-fill-start'), {'ticket_id': '55', 'url': 'https://lf.x/r'}, format='json')
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_start_400_when_flag_off(api, settings_obj):
    settings_obj.form_filling_enabled = False; settings_obj.save()
    Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    resp = api.post(reverse('zd-form-fill-start'), {'ticket_id': '55', 'url': 'https://lf.x/r'},
                    format='json', **_auth())
    assert resp.status_code == 400


@pytest.mark.django_db
def test_start_400_when_no_claim(api, settings_obj):
    resp = api.post(reverse('zd-form-fill-start'), {'ticket_id': '999', 'url': 'https://lf.x/r'},
                    format='json', **_auth())
    assert resp.status_code == 400


@pytest.mark.django_db
def test_start_creates_formfill_and_returns_session(api, settings_obj):
    Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1', client_name='Jo')
    with patch('apps.integrations.views.form_fill.browser_use.create_session',
               return_value={'id': 'S1', 'live_url': 'https://live/s1', 'status': 'running'}) as m:
        resp = api.post(reverse('zd-form-fill-start'),
                        {'ticket_id': '55', 'url': 'https://lf.example/r', 'post_screenshot': True},
                        format='json', **_auth())
    assert resp.status_code == 200
    assert resp.data['session_id'] == 'S1'
    assert resp.data['live_url'] == 'https://live/s1'
    ff = FormFill.objects.get(id=resp.data['form_fill_id'])
    assert ff.status == FormFill.STATUS_STARTED
    assert ff.browser_use_session_id == 'S1'
    # the task passed to Browser Use must not contain the real client name (PII via secrets only)
    assert 'Jo' not in m.call_args[1]['task']


@pytest.mark.django_db
def test_status_marks_filled_when_idle(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_STARTED)
    with patch('apps.integrations.views.form_fill.browser_use.get_session',
               return_value={'status': 'idle', 'output': 'filled', 'screenshot_url': '', 'is_successful': None}), \
         patch('apps.integrations.views.form_fill.browser_use.latest_screenshot_url', return_value=''):
        resp = api.post(reverse('zd-form-fill-status'), {'session_id': 'S1'}, format='json', **_auth())
    assert resp.status_code == 200
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_FILLED


@pytest.mark.django_db
def test_submit_advances_and_skips_note_when_not_requested(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_FILLED)
    with patch('apps.integrations.views.form_fill.browser_use.continue_session', return_value={'id': 'S1'}), \
         patch('apps.integrations.views.form_fill.browser_use.get_session',
               return_value={'status': 'stopped', 'output': 'Submitted, ref 123', 'screenshot_url': '', 'is_successful': True}), \
         patch('apps.integrations.views.form_fill.browser_use.latest_screenshot_url', return_value=''), \
         patch('apps.integrations.views.form_fill.post_zendesk_comment') as note:
        resp = api.post(reverse('zd-form-fill-submit'),
                        {'session_id': 'S1', 'ticket_id': '55', 'post_screenshot': False},
                        format='json', **_auth())
    assert resp.status_code == 200
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_SUBMITTED
    assert note.called is False


@pytest.mark.django_db
def test_cancel_stops_session(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_STARTED)
    with patch('apps.integrations.views.form_fill.browser_use.stop_session') as stop:
        resp = api.post(reverse('zd-form-fill-cancel'), {'session_id': 'S1'}, format='json', **_auth())
    assert resp.status_code == 200
    stop.assert_called_once()
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_CANCELLED
