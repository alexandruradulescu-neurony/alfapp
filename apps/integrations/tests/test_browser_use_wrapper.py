import json
import pytest
from unittest.mock import patch, MagicMock
from apps.config.models import SystemSettings
from apps.integrations import browser_use as bu


@pytest.fixture
def key(db):
    ss = SystemSettings.get_instance()
    ss.browser_use_api_key = 'bu_test'; ss.browser_use_model = 'claude-sonnet-4.6'; ss.save()
    return ss


def _resp(payload, status_code=200):
    m = MagicMock(); m.status_code = status_code; m.json.return_value = payload
    m.text = json.dumps(payload); return m


@pytest.mark.django_db
def test_create_session_sends_keepalive_secrets_and_normalizes_liveurl(key):
    with patch.object(bu.requests, 'post',
                      return_value=_resp({'id': 'S1', 'liveUrl': 'https://live/s1', 'status': 'running'}, 201)) as p:
        out = bu.create_session(task='fill it', secrets={'lf.example': {'x_name': 'Jo'}},
                                allowed_domains=['lf.example'])
    assert out['id'] == 'S1'
    assert out['live_url'] == 'https://live/s1'   # normalized from camelCase liveUrl
    body = p.call_args[1]['json']
    assert body['task'] == 'fill it'
    assert body['secrets'] == {'lf.example': {'x_name': 'Jo'}}
    assert body['allowed_domains'] == ['lf.example']
    assert body['enable_recording'] is False     # no full recording (PII); screenshot is the artifact
    assert body['keep_alive'] is True            # keeps the session idle for the follow-up
    assert body['model'] == 'claude-sonnet-4.6'


@pytest.mark.django_db
def test_continue_session_posts_session_id_without_keepalive(key):
    with patch.object(bu.requests, 'post', return_value=_resp({'id': 'S1', 'status': 'running'})) as p:
        bu.continue_session('S1', task='now submit')
    body = p.call_args[1]['json']
    assert body['session_id'] == 'S1' and body['task'] == 'now submit'
    assert 'keep_alive' not in body             # final action; let it stop after


@pytest.mark.django_db
def test_get_session_normalizes_screenshot_and_status(key):
    with patch.object(bu.requests, 'get',
                      return_value=_resp({'status': 'idle', 'output': 'done',
                                          'screenshotUrl': 'https://shot/1', 'isTaskSuccessful': True})):
        st = bu.get_session('S1')
    assert st['status'] == 'idle' and st['output'] == 'done'
    assert st['screenshot_url'] == 'https://shot/1'
    assert st['is_successful'] is True


@pytest.mark.django_db
def test_latest_screenshot_prefers_session_level(key):
    with patch.object(bu.requests, 'get',
                      return_value=_resp({'status': 'idle', 'screenshotUrl': 'https://shot/sess'})):
        assert bu.latest_screenshot_url('S1') == 'https://shot/sess'


@pytest.mark.django_db
def test_stop_session_calls_stop_endpoint(key):
    with patch.object(bu.requests, 'post', return_value=_resp({})) as p:
        bu.stop_session('S1')
    assert p.call_args[0][0] == 'https://api.browser-use.com/api/v3/sessions/S1/stop'
    assert p.call_args[1]['json']['strategy'] == 'session'


@pytest.mark.django_db
def test_missing_key_raises(db):
    ss = SystemSettings.get_instance(); ss.browser_use_api_key = ''; ss.save()
    with pytest.raises(bu.BrowserUseError):
        bu.create_session(task='x', secrets={}, allowed_domains=[])


@pytest.mark.django_db
def test_http_error_raises(key):
    with patch.object(bu.requests, 'post', return_value=_resp({'detail': 'bad'}, 400)):
        with pytest.raises(bu.BrowserUseError):
            bu.create_session(task='x', secrets={}, allowed_domains=[])
