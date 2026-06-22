"""Thin wrapper over the Browser Use Cloud API (v3). One place for every call; the
API key is read from SystemSettings and never logged. Requests are snake_case;
responses are camelCase and normalized here to snake_case. Raises BrowserUseError
on any failure so callers map it to a friendly message.

Confirmed shapes: see docs/superpowers/notes/browser-use-api-confirmed.md."""
import logging
import requests

from apps.config.models import SystemSettings

logger = logging.getLogger(__name__)

BASE_URL = 'https://api.browser-use.com/api/v3'
_TIMEOUT = 60


class BrowserUseError(Exception):
    """Any Browser Use call failed (no key, HTTP error, transport error)."""


def _key() -> str:
    key = SystemSettings.get_instance().browser_use_api_key or ''
    if not key:
        raise BrowserUseError('Browser Use API key is not configured in Settings.')
    return key


def _headers() -> dict:
    return {'X-Browser-Use-API-Key': _key(), 'Content-Type': 'application/json'}


def _check(resp) -> dict:
    if resp.status_code >= 400:
        body = (resp.text or '')[:300]
        logger.warning('Browser Use HTTP %s: %s', resp.status_code, body)
        raise BrowserUseError(f'Browser Use returned HTTP {resp.status_code}.')
    try:
        return resp.json()
    except ValueError:
        return {}


def _post(path: str, body: dict) -> dict:
    try:
        return _check(requests.post(f'{BASE_URL}{path}', headers=_headers(), json=body, timeout=_TIMEOUT))
    except requests.RequestException as e:
        raise BrowserUseError(f'Could not reach Browser Use: {e}') from e


def _get(path: str) -> dict:
    try:
        return _check(requests.get(f'{BASE_URL}{path}', headers=_headers(), timeout=_TIMEOUT))
    except requests.RequestException as e:
        raise BrowserUseError(f'Could not reach Browser Use: {e}') from e


def create_session(*, task: str, secrets: dict, allowed_domains: list,
                   enable_recording: bool = True, keep_alive: bool = True) -> dict:
    """Start a keep-alive session running the fill task. keep_alive=True keeps the
    session IDLE after the task so the approve->submit follow-up is accepted.
    Returns normalized {'id', 'live_url', 'status', 'raw'}."""
    body = {'task': task, 'secrets': secrets, 'allowed_domains': allowed_domains,
            'enable_recording': enable_recording, 'keep_alive': keep_alive}
    model = SystemSettings.get_instance().browser_use_model or ''
    if model:
        body['model'] = model
    data = _post('/sessions', body)
    return {'id': data.get('id', ''), 'live_url': data.get('liveUrl', ''),
            'status': data.get('status', ''), 'raw': data}


def continue_session(session_id: str, *, task: str) -> dict:
    """Send a follow-up task to an existing IDLE session (e.g. the submit step). No
    keep_alive — this is the final action; the session may stop afterwards."""
    data = _post('/sessions', {'task': task, 'session_id': session_id})
    return {'id': data.get('id', ''), 'status': data.get('status', ''), 'raw': data}


def get_session(session_id: str) -> dict:
    """Normalized session state: {'status','output','screenshot_url','is_successful','raw'}."""
    data = _get(f'/sessions/{session_id}')
    return {'status': data.get('status', ''), 'output': data.get('output') or '',
            'screenshot_url': data.get('screenshotUrl') or '',
            'is_successful': data.get('isTaskSuccessful'), 'raw': data}


def latest_screenshot_url(session_id: str) -> str:
    """Best-effort newest screenshot URL: the session-level screenshotUrl, else the
    newest message's screenshotUrl, else ''."""
    sess = get_session(session_id)
    if sess['screenshot_url']:
        return sess['screenshot_url']
    try:
        data = _get(f'/sessions/{session_id}/messages')
    except BrowserUseError:
        return ''
    msgs = data.get('messages', []) if isinstance(data, dict) else (data or [])
    shots = [m.get('screenshotUrl') for m in msgs if isinstance(m, dict) and m.get('screenshotUrl')]
    return shots[-1] if shots else ''


def stop_session(session_id: str, *, strategy: str = 'session') -> dict:
    """Stop/close the session (cleanup). Safe to call on an already-stopped session."""
    return _post(f'/sessions/{session_id}/stop', {'strategy': strategy})


def upload_file(session_id: str, *, filename: str, content: bytes, content_type: str) -> str:
    """Make a file available to the session for a form file input; returns the file
    name the agent references. Best-effort presigned flow (ask for an upload URL,
    then PUT the bytes). NOTE: the exact field names are pinned during the live
    smoke test (Task 9) — adjust here if the live API differs. Callers mock this in
    tests, so the contract (returns the filename) is what matters."""
    meta = _post(f'/sessions/{session_id}/files',
                 {'file_name': filename, 'content_type': content_type, 'size_bytes': len(content)})
    upload_url = meta.get('uploadUrl') or meta.get('url') or meta.get('presignedUrl') or ''
    if upload_url:
        try:
            put = requests.put(upload_url, data=content,
                               headers={'Content-Type': content_type}, timeout=120)
        except requests.RequestException as e:
            raise BrowserUseError(f'File upload failed: {e}') from e
        if put.status_code >= 400:
            raise BrowserUseError(f'File upload PUT returned HTTP {put.status_code}.')
    return filename
