import hashlib
import hmac
import io
import json
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
    Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1',
                         client_name='Jo', email_alias='alias-55@mailapptoday.com')
    with patch('apps.integrations.views.form_fill.fetch_zendesk_ticket', return_value={}), \
         patch('apps.integrations.views.form_fill.fetch_zendesk_comments', return_value=[]), \
         patch('apps.integrations.views.form_fill.browser_use.create_session',
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
    # real client email must not appear in secrets
    secrets = m.call_args[1]['secrets']
    assert 'c@e.com' not in list(secrets.get('lf.example', {}).values())


@pytest.mark.django_db
def test_start_uses_alias_for_email_not_real_email(api, settings_obj):
    Claim.objects.create(client_email='real@e.com', client_name='Jo', zd_ticket_id='55',
                         alf_claim_id='ALF1', email_alias='alias-55@mailapptoday.com')
    with patch('apps.integrations.views.form_fill.fetch_zendesk_ticket', return_value={}), \
         patch('apps.integrations.views.form_fill.fetch_zendesk_comments', return_value=[]), \
         patch('apps.integrations.views.form_fill.browser_use.create_session',
               return_value={'id': 'S1', 'live_url': 'x', 'status': 'running'}) as m:
        resp = api.post(reverse('zd-form-fill-start'),
                        {'ticket_id': '55', 'url': 'https://lf.example/r'}, format='json', **_auth())
    assert resp.status_code == 200
    secrets = m.call_args[1]['secrets']
    vals = list(secrets.get('lf.example', {}).values())
    assert 'alias-55@mailapptoday.com' in vals       # alias used
    assert 'real@e.com' not in vals                  # real email NOT sent


@pytest.mark.django_db
def test_start_uses_structured_profile_secrets_and_facts(api, settings_obj):
    from apps.integrations.form_profile import FormProfile
    claim = Claim.objects.create(client_email='real@e.com', client_name='Bronach Brother',
                                 zd_ticket_id='55', alf_claim_id='ALF1',
                                 email_alias='alias-55@mailapptoday.com')
    prof = FormProfile(first_name='Bronach', last_name='Brother',
                       email_alias='alias-55@mailapptoday.com', item_type='Suitcase',
                       baggage_tag='0081234567', item_description='green bag')
    with patch('apps.integrations.views.form_fill.fetch_zendesk_ticket', return_value={}), \
         patch('apps.integrations.views.form_fill.fetch_zendesk_comments', return_value=[]), \
         patch('apps.integrations.views.form_fill.build_form_profile', return_value=prof), \
         patch('apps.integrations.views.form_fill.browser_use.create_session',
               return_value={'id': 'S1', 'live_url': 'x', 'status': 'running'}) as m:
        resp = api.post(reverse('zd-form-fill-start'),
                        {'ticket_id': '55', 'url': 'https://lf.example/r'}, format='json', **_auth())
    assert resp.status_code == 200
    secrets = m.call_args[1]['secrets']['lf.example']
    assert secrets['x_baggage_tag'] == '0081234567'          # recovered ID reaches the form
    assert 'real@e.com' not in secrets.values()              # real email never sent
    task = m.call_args[1]['task']
    assert 'Item type: Suitcase' in task                     # visible fact shown in the brief
    assert 'Bronach' not in task                             # PII not inline in the brief
    claim.refresh_from_db()
    assert claim.form_profile.get('baggage_tag') == '0081234567'   # cached on the claim


@pytest.mark.django_db
def test_start_injects_site_playbook_into_brief(api, settings_obj):
    from apps.integrations.models import FormPlaybook
    FormPlaybook.objects.create(domain='lf.example', label='LF',
                                instructions='Tick both consent boxes before submit.', enabled=True)
    Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1',
                         email_alias='alias-55@mailapptoday.com')
    with patch('apps.integrations.views.form_fill.fetch_zendesk_ticket', return_value={}), \
         patch('apps.integrations.views.form_fill.fetch_zendesk_comments', return_value=[]), \
         patch('apps.integrations.views.form_fill.browser_use.create_session',
               return_value={'id': 'S1', 'live_url': 'x', 'status': 'running'}) as m:
        resp = api.post(reverse('zd-form-fill-start'),
                        {'ticket_id': '55', 'url': 'https://lf.example/r'}, format='json', **_auth())
    assert resp.status_code == 200
    assert 'Tick both consent boxes before submit.' in m.call_args[1]['task']   # playbook injected


@pytest.mark.django_db
def test_start_rejects_non_zendesk_image_url(api, settings_obj):
    settings_obj.zd_subdomain = 'airportlf'; settings_obj.save()
    Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    with patch('apps.integrations.views.form_fill.fetch_zendesk_attachment') as fetch, \
         patch('apps.integrations.views.form_fill.fetch_zendesk_ticket', return_value={}), \
         patch('apps.integrations.views.form_fill.fetch_zendesk_comments', return_value=[]), \
         patch('apps.integrations.views.form_fill.browser_use.create_session',
               return_value={'id': 'S1', 'live_url': 'x', 'status': 'running'}):
        resp = api.post(reverse('zd-form-fill-start'),
                        {'ticket_id': '55', 'url': 'https://lf.example/r',
                         'image_url': 'http://169.254.169.254/latest/meta-data/',
                         'image_filename': 'x.jpg'},
                        format='json', **_auth())
    assert resp.status_code == 200          # the fill still proceeds…
    fetch.assert_not_called()               # …but the SSRF fetch is blocked


@pytest.mark.django_db
def test_status_marks_filled_when_idle(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_STARTED)
    with patch('apps.integrations.views.form_fill.browser_use.get_session',
               return_value={'status': 'idle', 'output': 'filled', 'screenshot_url': '', 'is_successful': None}), \
         patch('apps.integrations.views.form_fill.browser_use.latest_screenshot_url', return_value=''), \
         patch('apps.integrations.views.form_fill.post_zendesk_comment') as note:
        resp = api.post(reverse('zd-form-fill-status'), {'session_id': 'S1'}, format='json', **_auth())
    assert resp.status_code == 200
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_FILLED
    note.assert_called_once()                        # ticket gets a "needs review" note


@pytest.mark.django_db
def test_status_stops_runaway_fill_over_step_budget(api, settings_obj):
    from apps.integrations.form_fill_service import MAX_FILL_STEPS
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_STARTED)
    with patch('apps.integrations.views.form_fill.browser_use.get_session',
               return_value={'status': 'running', 'output': '', 'screenshot_url': '',
                             'step_count': MAX_FILL_STEPS + 5, 'is_successful': None}), \
         patch('apps.integrations.views.form_fill.browser_use.latest_screenshot_url', return_value=''), \
         patch('apps.integrations.views.form_fill.browser_use.stop_session') as stop:
        resp = api.post(reverse('zd-form-fill-status'), {'session_id': 'S1'}, format='json', **_auth())
    assert resp.status_code == 200
    stop.assert_called_once()                       # runaway session stopped to cap cost
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_FAILED


@pytest.mark.django_db
def test_status_lets_fill_under_budget_keep_running(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_STARTED)
    with patch('apps.integrations.views.form_fill.browser_use.get_session',
               return_value={'status': 'running', 'output': '', 'screenshot_url': '',
                             'step_count': 5, 'is_successful': None}), \
         patch('apps.integrations.views.form_fill.browser_use.latest_screenshot_url', return_value=''), \
         patch('apps.integrations.views.form_fill.browser_use.stop_session') as stop:
        resp = api.post(reverse('zd-form-fill-status'), {'session_id': 'S1'}, format='json', **_auth())
    assert resp.status_code == 200
    stop.assert_not_called()
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_STARTED     # still filling


@pytest.mark.django_db
def test_submit_advances_and_skips_note_when_not_requested(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_FILLED)
    with patch('apps.integrations.views.form_fill.browser_use.continue_session', return_value={'id': 'S1'}), \
         patch('apps.integrations.views.form_fill.post_zendesk_comment') as note:
        resp = api.post(reverse('zd-form-fill-submit'),
                        {'session_id': 'S1', 'ticket_id': '55', 'post_screenshot': False},
                        format='json', **_auth())
    assert resp.status_code == 200
    assert resp.data['status'] == 'submitting'      # submit only kicks off; status poll finalizes
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_SUBMITTING   # NOT submitted yet
    assert note.called is False                      # no note at submit time


@pytest.mark.django_db
def test_submit_rejected_when_not_filled(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                            browser_use_session_id='S1', status=FormFill.STATUS_STARTED)
    with patch('apps.integrations.views.form_fill.browser_use.continue_session') as cont:
        resp = api.post(reverse('zd-form-fill-submit'),
                        {'session_id': 'S1', 'ticket_id': '55'}, format='json', **_auth())
    assert resp.status_code == 400
    cont.assert_not_called()


@pytest.mark.django_db
def test_status_finalizes_submit_and_posts_note_when_stopped(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_SUBMITTING,
                                 post_screenshot=True)
    with patch('apps.integrations.views.form_fill.browser_use.get_session',
               return_value={'status': 'stopped', 'output': 'Submitted ref 9', 'screenshot_url': 'x', 'is_successful': True}), \
         patch('apps.integrations.views.form_fill._proxy_screenshot', return_value='data:image/png;base64,zzz'), \
         patch('apps.integrations.views.form_fill.post_zendesk_comment') as note:
        resp = api.post(reverse('zd-form-fill-status'), {'session_id': 'S1'}, format='json', **_auth())
    assert resp.status_code == 200
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_SUBMITTED
    assert ff.posted_to_ticket is True
    note.assert_called_once()


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


@pytest.mark.django_db
def test_attachments_lists_only_image_attachments(api, settings_obj):
    Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    fake_comments = [{'attachments': [
        {'file_name': 'item.jpg', 'content_type': 'image/jpeg', 'content_url': 'https://zd/att/1'},
        {'file_name': 'note.pdf', 'content_type': 'application/pdf', 'content_url': 'https://zd/att/2'},
    ]}]
    with patch('apps.integrations.views.form_fill.fetch_zendesk_comments', return_value=fake_comments):
        resp = api.post(reverse('zd-form-fill-attachments'), {'ticket_id': '55'}, format='json', **_auth())
    assert resp.status_code == 200
    names = [a['filename'] for a in resp.data['attachments']]
    assert 'item.jpg' in names and 'note.pdf' not in names


@pytest.mark.django_db
def test_upload_image_stores_on_formfill(api, settings_obj):
    Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    from django.core.files.uploadedfile import SimpleUploadedFile
    upload = SimpleUploadedFile('p.jpg', b'\xff\xd8\xff\xe0fakejpeg', content_type='image/jpeg')
    resp = api.post(reverse('zd-form-fill-upload'),
                    {'ticket_id': '55', 'image': upload}, format='multipart', **_auth())
    assert resp.status_code == 200
    ff = FormFill.objects.get(id=resp.data['form_fill_id'])
    assert ff.image_source == FormFill.IMAGE_SOURCE_UPLOAD
    assert ff.image_name == 'p.jpg'


@pytest.mark.django_db
def test_start_with_uploaded_image_uploads_to_session(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    from django.core.files.base import ContentFile
    ff = FormFill.objects.create(claim=claim, form_url='', status=FormFill.STATUS_STARTED,
                                 image_source=FormFill.IMAGE_SOURCE_UPLOAD, image_name='p.jpg')
    ff.image.save('p.jpg', ContentFile(b'\xff\xd8fake'), save=True)
    with patch('apps.integrations.views.form_fill.fetch_zendesk_ticket', return_value={}), \
         patch('apps.integrations.views.form_fill.fetch_zendesk_comments', return_value=[]), \
         patch('apps.integrations.views.form_fill.browser_use.create_session',
               return_value={'id': 'S9', 'live_url': 'https://live/s9', 'status': 'running'}), \
         patch('apps.integrations.views.form_fill.browser_use.upload_file', return_value='p.jpg') as up:
        resp = api.post(reverse('zd-form-fill-start'),
                        {'ticket_id': '55', 'url': 'https://lf.example/r', 'form_fill_id': ff.id},
                        format='json', **_auth())
    assert resp.status_code == 200
    assert resp.data['form_fill_id'] == ff.id      # reused the uploaded row
    up.assert_called_once()                        # image pushed to the session


# --- Browser Use webhook receiver ---

WEBHOOK_SECRET = 'bu_whsec_test'


def _sign(body: bytes, ts: str, secret=WEBHOOK_SECRET):
    # Browser Use signs '{timestamp}.{canonical_json}' (sorted keys, compact separators).
    message = f"{ts}.{json.dumps(json.loads(body), separators=(',', ':'), sort_keys=True)}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _event_body():
    return json.dumps({'type': 'session.status.update', 'timestamp': 'iso',
                       'payload': {'session_id': 'S1', 'status': 'stopped'}}).encode()


@pytest.mark.django_db
def test_webhook_test_event_returns_200_even_without_secret(api, settings_obj):
    # the 'test' event fires at creation, before the code is pasted into Settings → must 200
    body = json.dumps({'type': 'test', 'timestamp': 't', 'payload': {'test': 'ok'}})
    resp = api.post(reverse('browser-use-webhook'), data=body, content_type='application/json')
    assert resp.status_code == 200


@pytest.mark.django_db
def test_webhook_status_update_finalizes_and_notifies_with_valid_signature(api, settings_obj):
    settings_obj.browser_use_webhook_secret = WEBHOOK_SECRET; settings_obj.save()
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_STARTED)
    body = _event_body()
    ts = '1737406233'
    with patch('apps.integrations.views.form_fill.browser_use.get_session',
               return_value={'status': 'idle', 'output': 'filled', 'screenshot_url': '', 'is_successful': None}), \
         patch('apps.integrations.views.form_fill._proxy_screenshot', return_value=''), \
         patch('apps.integrations.views.form_fill.post_zendesk_comment') as note:
        resp = api.post(reverse('browser-use-webhook'), data=body, content_type='application/json',
                        HTTP_X_BROWSER_USE_SIGNATURE=_sign(body, ts), HTTP_X_BROWSER_USE_TIMESTAMP=ts)
    assert resp.status_code == 200
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_FILLED       # finalized server-side, no tab/poll needed
    note.assert_called_once()                        # and the ticket gets a "needs review" note


@pytest.mark.django_db
def test_webhook_rejects_bad_signature(api, settings_obj):
    settings_obj.browser_use_webhook_secret = WEBHOOK_SECRET; settings_obj.save()
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.x/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_STARTED)
    with patch('apps.integrations.views.form_fill.browser_use.get_session') as gs:
        resp = api.post(reverse('browser-use-webhook'), data=_event_body(), content_type='application/json',
                        HTTP_X_BROWSER_USE_SIGNATURE='deadbeef', HTTP_X_BROWSER_USE_TIMESTAMP='1')
    assert resp.status_code == 401
    gs.assert_not_called()
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_STARTED      # spoofed call changes nothing


@pytest.mark.django_db
def test_webhook_real_event_rejected_when_no_secret_configured(api, settings_obj):
    # secret not pasted yet → fail closed on a real (non-test) event
    body = _event_body()
    resp = api.post(reverse('browser-use-webhook'), data=body, content_type='application/json',
                    HTTP_X_BROWSER_USE_SIGNATURE=_sign(body, '1'), HTTP_X_BROWSER_USE_TIMESTAMP='1')
    assert resp.status_code == 401
