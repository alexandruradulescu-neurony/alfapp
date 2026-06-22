# Zendesk "Form Filling" (Browser Use Cloud) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Zendesk-sidebar "Form filling" tab where an agent pastes an institution form URL and an AI browser agent (Browser Use Cloud) fills it from the linked LORA claim, pauses for human approval, then submits and screenshots the confirmation — every attempt recorded as a per-claim `FormFill` audit row.

**Architecture:** LORA (Django, on Railway) is the orchestrator: the sidebar tab calls LORA endpoints; LORA talks to Browser Use Cloud over HTTPS (raw `requests`), builds the form data as domain-scoped "secrets" (so the LLM never sees PII), proxies screenshots back, and posts the confirmation screenshot to the ticket. Persistence is a new `FormFill` model in `apps/integrations`. Feature is OFF by default behind a `SystemSettings` flag. Spec: `docs/superpowers/specs/2026-06-22-zendesk-form-filling-browser-use-design.md`.

**Tech Stack:** Django + DRF (`APIView`), `requests`, `EncryptedCharField`, Browser Use Cloud API v3, the existing Zendesk sidebar app (vanilla JS, prod CSP forbids eval/Alpine), pytest.

---

## File Structure

- `apps/config/models.py` — **modify**: 3 new `SystemSettings` fields (key, model, flag).
- `apps/config/migrations/0032_*.py` — **create** (auto): the settings migration.
- `apps/config/forms.py` — **modify**: register the new fields.
- `templates/manager/settings.html` — **modify**: render the new fields.
- `apps/integrations/models.py` — **modify**: the `FormFill` model.
- `apps/integrations/migrations/0001_initial.py` — **create** (auto): first integrations migration.
- `apps/integrations/browser_use.py` — **create**: thin HTTP wrapper over Browser Use Cloud.
- `apps/integrations/form_fill_service.py` — **create**: claim→secrets/task builder + orchestration helpers.
- `apps/integrations/views/form_fill.py` — **create**: the 6 sidebar endpoints.
- `apps/integrations/views/__init__.py` — **modify**: re-export the new views.
- `apps/integrations/urls.py` — **modify**: route the new endpoints.
- `zendesk_app/assets/iframe.html` — **modify**: new "Form filling" tab + panel.
- `zendesk_app/assets/app.js` — **modify**: the tab's JS.
- `templates/manager/_claim_form_fills.html` + claim detail view — **modify**: per-claim history panel.
- Tests: `apps/integrations/tests/test_form_fill_model.py`, `test_browser_use_wrapper.py`, `test_form_fill_service.py`, `test_form_fill_endpoints.py`; `apps/users/tests/test_settings_browser_use.py`.

**Test runner (this repo):** `.venv/bin/python -m pytest <path> -o addopts=""` (sqlite; don't run many in parallel).

**Browser Use Cloud facts (from the spec / docs):** auth header `X-Browser-Use-API-Key: bu_…`; create session `POST https://api.browser-use.com/api/v3/sessions` with `{task, secrets, allowed_domains, enable_recording, model}` → `{id, live_url}`; status `GET /api/v3/sessions/{id}` → `{status, output}`; messages `GET /api/v3/sessions/{id}/messages` carry `screenshot_url`; follow-up = another task on the same `session_id`; stop = `POST /api/v3/sessions/{id}/stop`. Exact screenshot-retrieval, file-upload, and `secrets` shapes are pinned in **Task 1** against the live key.

---

## Task 1: Spike — pin the Browser Use Cloud API against the live key

No code ships in this task. It records the exact request/response shapes the wrapper (Task 4) depends on, so the wrapper is built against reality, not guesses. The user has a Browser Use API key.

**Files:**
- Create: `docs/superpowers/notes/browser-use-api-confirmed.md` (scratch findings).

- [ ] **Step 1: Export the key locally (do not commit it)**

```bash
export BROWSER_USE_API_KEY=bu_xxx   # the user's key
```

- [ ] **Step 2: Create a session and confirm the create shape + live_url**

```bash
curl -s -X POST https://api.browser-use.com/api/v3/sessions \
  -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task":"go to https://example.com and report the page title","enable_recording":true}' | tee /tmp/bu_create.json
```

Record: the JSON field that holds the session id (`id`?), the `live_url` field name, and the exact base path (`/api/v3/sessions`).

- [ ] **Step 3: Poll status + find the screenshot**

```bash
SID=$(python3 -c "import json;print(json.load(open('/tmp/bu_create.json'))['id'])")
curl -s https://api.browser-use.com/api/v3/sessions/$SID \
  -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" | tee /tmp/bu_status.json
curl -s https://api.browser-use.com/api/v3/sessions/$SID/messages \
  -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" | tee /tmp/bu_msgs.json
```

Record: the `status` values, the field carrying the agent's result (`output`?), and where a `screenshot_url` (or screenshot bytes) appears. If `/messages` does not yield a usable still, note the v1 fallback `GET /api/v1/task/{id}/screenshots`.

- [ ] **Step 4: Confirm a follow-up task on the same session**

```bash
curl -s -X POST https://api.browser-use.com/api/v3/sessions \
  -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" -H "Content-Type: application/json" \
  -d "{\"task\":\"now scroll to the bottom\",\"session_id\":\"$SID\"}"
```

Record: whether `session_id` is the correct key to continue a session, or whether v3 uses a different continuation route.

- [ ] **Step 5: Confirm secrets + file-upload shapes**

Confirm the **secrets** shape for multiple named placeholders (string `"host":"k:v"` vs nested `"host":{"x_name":"val"}`). Confirm **file upload**: v3 workspaces (`workspaces.upload` + `workspace_id`) vs v2 session files (`POST /api/v2/sessions/{id}/files` presigned, ≤10 MB). Document the chosen path.

- [ ] **Step 6: Write findings + stop the test session**

Fill `docs/superpowers/notes/browser-use-api-confirmed.md` with the confirmed: base URL, auth header, create body, status/result fields, screenshot retrieval, follow-up mechanism, secrets shape, file-upload path. Then:

```bash
curl -s -X POST https://api.browser-use.com/api/v3/sessions/$SID/stop \
  -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" -d '{"strategy":"session"}'
```

- [ ] **Step 7: Commit the note**

```bash
git add docs/superpowers/notes/browser-use-api-confirmed.md
git commit -m "docs: confirmed Browser Use Cloud API shapes for form filling"
```

> Tasks 4+ reference "the confirmed shape" — use this note. If a shape differs from this plan's assumptions, adjust the wrapper code (the tests assert the request *we* build, so update test + code together).

---

## Task 2: SystemSettings — Browser Use config (key, model, flag)

**Files:**
- Modify: `apps/config/models.py` (SystemSettings class)
- Modify: `apps/config/forms.py`
- Modify: `templates/manager/settings.html`
- Create (auto): `apps/config/migrations/0032_*.py`
- Test: `apps/users/tests/test_settings_browser_use.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/users/tests/test_settings_browser_use.py
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
        # Build a POST that includes the existing form fields plus ours. Pull the
        # current values so we don't blank required fields.
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
        data['browser_use_api_key'] = ''  # blank → must NOT wipe
        c.post(reverse('manager_settings'), data)
        ss.refresh_from_db()
        assert ss.browser_use_api_key == 'bu_keep_me'
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest apps/users/tests/test_settings_browser_use.py -o addopts="" -q`
Expected: FAIL (`AttributeError`/missing fields).

- [ ] **Step 3: Add the model fields**

In `apps/config/models.py`, inside `class SystemSettings`, near the other API-key/flag fields (e.g. after `aerodatabox_api_key`), add:

```python
    # --- Browser Use Cloud (Zendesk "Form filling" feature) ---
    browser_use_api_key = EncryptedCharField(
        max_length=255, blank=True, default='',
        help_text='Browser Use Cloud API key (bu_…), encrypted at rest. Powers the '
                  'Zendesk Form filling tab.')
    browser_use_model = models.CharField(
        max_length=64, blank=True, default='claude-sonnet-4.6',
        help_text='Model Browser Use runs the form-filling agent on.')
    form_filling_enabled = models.BooleanField(
        default=False,
        help_text='When ON, the Zendesk Form filling tab can drive Browser Use to fill '
                  'institution forms from a claim. OFF by default.')
```

- [ ] **Step 4: Make + name the migration**

Run: `.venv/bin/python manage.py makemigrations config`
Expected: creates `apps/config/migrations/0032_*.py` adding the three fields.

- [ ] **Step 5: Register fields in the form**

In `apps/config/forms.py`: add `'browser_use_api_key'` to `SENSITIVE_FIELDS`; add `'browser_use_model'` and `'form_filling_enabled'` to `Meta.fields`.

- [ ] **Step 6: Render the fields on the settings page**

In `templates/manager/settings.html`, in a sensible section (e.g. near the AeroDataBox/flight block), add:

```html
<div class="sm:col-span-2">
  <label class="block text-sm font-medium text-gray-900">Browser Use API key</label>
  <input type="password" name="browser_use_api_key" autocomplete="off"
         class="block w-full rounded-md bg-white px-3 py-1.5 text-sm text-gray-900 outline-1 -outline-offset-1 outline-gray-300 focus:outline-2 focus:-outline-offset-2 focus:outline-indigo-600"
         placeholder="{% if settings.browser_use_api_key %}•••••••• (saved — leave blank to keep){% else %}bu_…{% endif %}">
</div>
<div class="sm:col-span-2">
  <label class="block text-sm font-medium text-gray-900">Browser Use model</label>
  <input type="text" name="browser_use_model" value="{{ settings.browser_use_model }}"
         class="block w-full rounded-md bg-white px-3 py-1.5 text-sm text-gray-900 outline-1 -outline-offset-1 outline-gray-300 focus:outline-2 focus:-outline-offset-2 focus:outline-indigo-600">
</div>
<div class="sm:col-span-2 flex items-start gap-2">
  <input type="checkbox" id="form_filling_enabled" name="form_filling_enabled"
         class="mt-0.5 size-5 rounded border-gray-300 accent-indigo-500"
         {% if settings.form_filling_enabled %}checked{% endif %}>
  <label for="form_filling_enabled" class="text-sm text-gray-700">
    Enable the Zendesk <strong>Form filling</strong> tab (Browser Use). Off by default.</label>
</div>
```

- [ ] **Step 7: Run the tests to green**

Run: `.venv/bin/python -m pytest apps/users/tests/test_settings_browser_use.py -o addopts="" -q`
Expected: PASS (3 tests). If `test_save_*` fails because the POST is missing other required fields, copy the field values from the rendered form as the test already does.

- [ ] **Step 8: Commit**

```bash
git add apps/config/models.py apps/config/migrations/0032_*.py apps/config/forms.py templates/manager/settings.html apps/users/tests/test_settings_browser_use.py
git commit -m "feat(settings): Browser Use API key, model, and form-filling flag (off by default)"
```

---

## Task 3: The `FormFill` model (per-claim audit)

**Files:**
- Modify: `apps/integrations/models.py`
- Create (auto): `apps/integrations/migrations/0001_initial.py`
- Test: `apps/integrations/tests/test_form_fill_model.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/integrations/tests/test_form_fill_model.py
import pytest
from apps.claims.models import Claim
from apps.integrations.models import FormFill


@pytest.mark.django_db
class TestFormFillModel:
    def _claim(self):
        return Claim.objects.create(client_email='c@e.com', alf_claim_id='ALF1', zd_ticket_id='100')

    def test_create_defaults(self):
        ff = FormFill.objects.create(claim=self._claim(), form_url='https://lf.example/report')
        assert ff.status == FormFill.STATUS_STARTED
        assert ff.image_source == FormFill.IMAGE_SOURCE_NONE
        assert ff.posted_to_ticket is False
        assert ff.created_at is not None

    def test_related_name_and_ordering(self):
        claim = self._claim()
        FormFill.objects.create(claim=claim, form_url='https://a')
        FormFill.objects.create(claim=claim, form_url='https://b')
        fills = list(claim.form_fills.all())
        assert len(fills) == 2
        assert fills[0].created_at >= fills[1].created_at  # newest first

    def test_status_choices_cover_lifecycle(self):
        vals = {c[0] for c in FormFill.STATUS_CHOICES}
        assert vals == {'STARTED', 'FILLED', 'SUBMITTED', 'CANCELLED', 'FAILED'}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_fill_model.py -o addopts="" -q`
Expected: FAIL (`ImportError: cannot import name 'FormFill'`).

- [ ] **Step 3: Implement the model**

Replace the placeholder body of `apps/integrations/models.py` with:

```python
from django.conf import settings
from django.db import models


class FormFill(models.Model):
    """One institution-form fill attempt, driven by Browser Use, recorded per claim.

    The durable audit trail (what form, when, by whom, the result + screenshot) and
    the source of truth the Zendesk tab / claim page read from."""

    STATUS_STARTED = 'STARTED'
    STATUS_FILLED = 'FILLED'
    STATUS_SUBMITTED = 'SUBMITTED'
    STATUS_CANCELLED = 'CANCELLED'
    STATUS_FAILED = 'FAILED'
    STATUS_CHOICES = [
        (STATUS_STARTED, 'Started'),
        (STATUS_FILLED, 'Filled — awaiting approval'),
        (STATUS_SUBMITTED, 'Submitted'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_FAILED, 'Failed'),
    ]

    IMAGE_SOURCE_NONE = 'none'
    IMAGE_SOURCE_TICKET = 'ticket'
    IMAGE_SOURCE_UPLOAD = 'upload'
    IMAGE_SOURCE_CHOICES = [
        (IMAGE_SOURCE_NONE, 'No image'),
        (IMAGE_SOURCE_TICKET, 'From ticket attachment'),
        (IMAGE_SOURCE_UPLOAD, 'Agent uploaded'),
    ]

    claim = models.ForeignKey('claims.Claim', on_delete=models.CASCADE,
                              related_name='form_fills')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')
    form_url = models.URLField(max_length=1000)
    browser_use_session_id = models.CharField(max_length=128, blank=True, default='')
    browser_use_workspace_id = models.CharField(max_length=128, blank=True, default='')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_STARTED)

    image_source = models.CharField(max_length=8, choices=IMAGE_SOURCE_CHOICES,
                                    default=IMAGE_SOURCE_NONE)
    image_name = models.CharField(max_length=255, blank=True, default='')
    # FileField (not ImageField) so we don't depend on Pillow.
    image = models.FileField(upload_to='form_fill_images/', null=True, blank=True)
    confirmation_screenshot = models.FileField(upload_to='form_fill_screenshots/',
                                               null=True, blank=True)

    result_output = models.TextField(blank=True, default='')
    error = models.TextField(blank=True, default='')
    posted_to_ticket = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    filled_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['claim', '-created_at'])]

    def __str__(self):
        return f"FormFill #{self.pk} ({self.status}) for claim {self.claim_id}"
```

- [ ] **Step 4: Make the migration**

Run: `.venv/bin/python manage.py makemigrations integrations`
Expected: creates `apps/integrations/migrations/0001_initial.py`. (If the `migrations/` dir is missing it is created automatically.)

- [ ] **Step 5: Run tests to green**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_fill_model.py -o addopts="" -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/integrations/models.py apps/integrations/migrations/0001_initial.py apps/integrations/tests/test_form_fill_model.py
git commit -m "feat(integrations): FormFill model — per-claim form-filling audit trail"
```

---

## Task 4: Browser Use Cloud wrapper

A single module that owns every Browser Use HTTP call. Uses `requests` (already used by the Anthropic path). Reads the key from `SystemSettings`. Raises `BrowserUseError` on failure; never logs the key. Use the shapes confirmed in Task 1 — the code below assumes the documented v3 shapes; adjust if Task 1 found otherwise (update test + code together).

**Files:**
- Create: `apps/integrations/browser_use.py`
- Test: `apps/integrations/tests/test_browser_use_wrapper.py`

- [ ] **Step 1: Write the failing tests**

```python
# apps/integrations/tests/test_browser_use_wrapper.py
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
    m.content = json.dumps(payload).encode(); return m


@pytest.mark.django_db
def test_create_session_sends_key_task_secrets(key):
    with patch.object(bu.requests, 'post', return_value=_resp({'id': 'S1', 'live_url': 'https://live/s1'})) as p:
        out = bu.create_session(task='fill it', secrets={'lf.example': {'x_name': 'Jo'}},
                                allowed_domains=['lf.example'])
    assert out['id'] == 'S1' and out['live_url'] == 'https://live/s1'
    url, kwargs = p.call_args[0][0], p.call_args[1]
    assert url == 'https://api.browser-use.com/api/v3/sessions'
    assert kwargs['headers']['X-Browser-Use-API-Key'] == 'bu_test'
    body = kwargs['json']
    assert body['task'] == 'fill it'
    assert body['secrets'] == {'lf.example': {'x_name': 'Jo'}}
    assert body['allowed_domains'] == ['lf.example']
    assert body['model'] == 'claude-sonnet-4.6'
    assert body['enable_recording'] is True


@pytest.mark.django_db
def test_continue_session_posts_followup_with_session_id(key):
    with patch.object(bu.requests, 'post', return_value=_resp({'id': 'S1'})) as p:
        bu.continue_session('S1', task='now submit')
    body = p.call_args[1]['json']
    assert body['session_id'] == 'S1' and body['task'] == 'now submit'


@pytest.mark.django_db
def test_get_status_parses(key):
    with patch.object(bu.requests, 'get', return_value=_resp({'status': 'idle', 'output': 'done'})):
        st = bu.get_session('S1')
    assert st['status'] == 'idle' and st['output'] == 'done'


@pytest.mark.django_db
def test_stop_session(key):
    with patch.object(bu.requests, 'post', return_value=_resp({})) as p:
        bu.stop_session('S1')
    assert p.call_args[0][0] == 'https://api.browser-use.com/api/v3/sessions/S1/stop'


@pytest.mark.django_db
def test_missing_key_raises(db):
    ss = SystemSettings.get_instance(); ss.browser_use_api_key = ''; ss.save()
    with pytest.raises(bu.BrowserUseError):
        bu.create_session(task='x', secrets={}, allowed_domains=[])


@pytest.mark.django_db
def test_http_error_raises_browseruseerror(key):
    with patch.object(bu.requests, 'post', return_value=_resp({'error': 'bad'}, status_code=400)):
        with pytest.raises(bu.BrowserUseError):
            bu.create_session(task='x', secrets={}, allowed_domains=[])
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_browser_use_wrapper.py -o addopts="" -q`
Expected: FAIL (`ModuleNotFoundError: apps.integrations.browser_use`).

- [ ] **Step 3: Implement the wrapper**

```python
# apps/integrations/browser_use.py
"""Thin wrapper over the Browser Use Cloud API (v3). One place for every call;
the API key is read from SystemSettings and never logged. Raises BrowserUseError
on any failure so callers map it to a friendly message."""
import logging
import requests

from apps.config.models import SystemSettings

logger = logging.getLogger(__name__)

BASE_URL = 'https://api.browser-use.com/api/v3'
_TIMEOUT = 60


class BrowserUseError(Exception):
    """Any Browser Use call failed (no key, HTTP error, bad payload)."""


def _key() -> str:
    key = SystemSettings.get_instance().browser_use_api_key or ''
    if not key:
        raise BrowserUseError('Browser Use API key is not configured in Settings.')
    return key


def _headers() -> dict:
    return {'X-Browser-Use-API-Key': _key(), 'Content-Type': 'application/json'}


def _check(resp) -> dict:
    if resp.status_code >= 400:
        # Never include the key; include status + short body for diagnosis.
        body = (resp.text or '')[:300]
        logger.warning('Browser Use HTTP %s: %s', resp.status_code, body)
        raise BrowserUseError(f'Browser Use returned HTTP {resp.status_code}.')
    try:
        return resp.json()
    except ValueError:
        return {}


def create_session(*, task: str, secrets: dict, allowed_domains: list,
                   enable_recording: bool = True) -> dict:
    """Start a session. Returns the parsed body (expects id + live_url)."""
    model = SystemSettings.get_instance().browser_use_model or 'claude-sonnet-4.6'
    body = {'task': task, 'secrets': secrets, 'allowed_domains': allowed_domains,
            'enable_recording': enable_recording, 'model': model}
    try:
        resp = requests.post(f'{BASE_URL}/sessions', headers=_headers(), json=body, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise BrowserUseError(f'Could not reach Browser Use: {e}') from e
    return _check(resp)


def continue_session(session_id: str, *, task: str) -> dict:
    """Send a follow-up task to an existing live session (e.g. 'now submit')."""
    body = {'task': task, 'session_id': session_id}
    try:
        resp = requests.post(f'{BASE_URL}/sessions', headers=_headers(), json=body, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise BrowserUseError(f'Could not reach Browser Use: {e}') from e
    return _check(resp)


def get_session(session_id: str) -> dict:
    try:
        resp = requests.get(f'{BASE_URL}/sessions/{session_id}', headers=_headers(), timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise BrowserUseError(f'Could not reach Browser Use: {e}') from e
    return _check(resp)


def get_messages(session_id: str) -> dict:
    try:
        resp = requests.get(f'{BASE_URL}/sessions/{session_id}/messages',
                            headers=_headers(), timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise BrowserUseError(f'Could not reach Browser Use: {e}') from e
    return _check(resp)


def latest_screenshot_url(session_id: str) -> str:
    """Best-effort: the newest screenshot_url from the messages stream, or ''.
    (Task 1 confirms the exact field; adjust the extraction here if it differs.)"""
    data = get_messages(session_id)
    msgs = data.get('messages', data if isinstance(data, list) else [])
    urls = [m.get('screenshot_url') for m in msgs if isinstance(m, dict) and m.get('screenshot_url')]
    return urls[-1] if urls else ''


def stop_session(session_id: str, *, strategy: str = 'session') -> dict:
    try:
        resp = requests.post(f'{BASE_URL}/sessions/{session_id}/stop',
                             headers=_headers(), json={'strategy': strategy}, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise BrowserUseError(f'Could not reach Browser Use: {e}') from e
    return _check(resp)


def upload_file(session_id: str, *, filename: str, content: bytes, content_type: str) -> str:
    """Make a file available to the session for a form file input. Returns the file
    reference the agent uses. Implementation pinned in Task 1 (v3 workspaces vs v2
    presigned session files); keep the call here so callers stay unchanged."""
    raise NotImplementedError('Pin the upload path in Task 1, then implement here.')
```

- [ ] **Step 4: Run tests to green**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_browser_use_wrapper.py -o addopts="" -q`
Expected: PASS (6 tests). `upload_file` is intentionally unimplemented until Task 7.

- [ ] **Step 5: Commit**

```bash
git add apps/integrations/browser_use.py apps/integrations/tests/test_browser_use_wrapper.py
git commit -m "feat(integrations): Browser Use Cloud wrapper (create/continue/status/screenshot/stop)"
```

---

## Task 5: Claim → secrets/task builder

Pure functions: build the domain-scoped placeholder secrets from a claim, and the fill / submit task strings. No PII appears in the task string — only placeholder names.

**Files:**
- Create: `apps/integrations/form_fill_service.py`
- Test: `apps/integrations/tests/test_form_fill_service.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/integrations/tests/test_form_fill_service.py
import pytest
from urllib.parse import urlparse
from apps.claims.models import Claim
from apps.integrations.form_fill_service import (
    build_form_secrets, build_fill_task, SUBMIT_TASK, form_host)


@pytest.mark.django_db
def test_build_secrets_includes_only_present_fields():
    claim = Claim.objects.create(
        client_email='jo@e.com', client_name='Jo Bloggs', alf_claim_id='ALF9',
        object_description='black Sony headphones', lost_location='JFK Terminal 4',
        flight_details='AA100 2026-06-01', zd_ticket_id='55')
    host = 'lf.example'
    secrets = build_form_secrets(claim, host)
    placeholders = secrets[host]
    assert placeholders['x_client_name'] == 'Jo Bloggs'
    assert placeholders['x_client_email'] == 'jo@e.com'
    assert placeholders['x_item_description'] == 'black Sony headphones'
    assert placeholders['x_lost_location'] == 'JFK Terminal 4'
    assert placeholders['x_claim_ref'] == 'ALF9'
    # phone is empty → omitted
    assert 'x_client_phone' not in placeholders


@pytest.mark.django_db
def test_fill_task_uses_placeholders_not_values_and_says_do_not_submit():
    claim = Claim.objects.create(client_email='jo@e.com', client_name='Jo Bloggs', alf_claim_id='ALF9')
    task = build_fill_task('https://lf.example/report', build_form_secrets(claim, 'lf.example'))
    assert 'Jo Bloggs' not in task and 'jo@e.com' not in task   # real PII never in the prompt
    assert 'x_client_name' in task and 'x_client_email' in task
    assert 'do not submit' in task.lower()
    assert 'https://lf.example/report' in task


def test_submit_task_is_explicit():
    assert 'submit' in SUBMIT_TASK.lower()


def test_form_host_extracts_domain():
    assert form_host('https://app.nettracer.aero/lf/report?x=1') == 'app.nettracer.aero'
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_fill_service.py -o addopts="" -q`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement**

```python
# apps/integrations/form_fill_service.py
"""Build the Browser Use task + domain-scoped secrets from a claim. The agent
(LLM) only ever sees placeholder NAMES (x_client_name, …); the real values are
filled into the form by Browser Use, never sent to the model."""
from urllib.parse import urlparse

# placeholder name -> Claim attribute
_FIELD_MAP = [
    ('x_client_name', 'client_name'),
    ('x_client_email', 'client_email'),
    ('x_client_phone', 'phone'),
    ('x_item_description', 'object_description'),
    ('x_lost_location', 'lost_location'),
    ('x_flight_details', 'flight_details'),
    ('x_incident_details', 'incident_details'),
    ('x_claim_ref', 'alf_claim_id'),
]

_LABELS = {
    'x_client_name': "the claimant's full name",
    'x_client_email': "the claimant's email",
    'x_client_phone': "the claimant's phone",
    'x_item_description': "the lost item's description",
    'x_lost_location': "where the item was lost",
    'x_flight_details': "the flight details",
    'x_incident_details': "how/when it was lost",
    'x_claim_ref': "the claim reference number",
}

SUBMIT_TASK = ("Submit the form now by clicking its submit/send button, then report "
               "the confirmation message or reference shown.")


def form_host(url: str) -> str:
    return (urlparse(url).hostname or '').lower()


def build_form_secrets(claim, host: str) -> dict:
    """Return {host: {placeholder: value}} for every non-empty claim field."""
    values = {}
    for placeholder, attr in _FIELD_MAP:
        val = (getattr(claim, attr, '') or '')
        val = str(val).strip()
        if val:
            values[placeholder] = val
    return {host: values}


def build_fill_task(url: str, secrets: dict) -> str:
    """The fill instruction. References placeholders only — never the real values."""
    host = next(iter(secrets), '')
    present = secrets.get(host, {})
    lines = [f"- {name}: {_LABELS.get(name, name)}" for name in present]
    fields = "\n".join(lines)
    return (
        f"Open the lost-item report form at {url} and fill it in. Use these secret "
        f"placeholder values for the matching fields (match by the form's own field "
        f"labels):\n{fields}\n"
        f"Leave any field you have no value for blank. IMPORTANT: do NOT submit the "
        f"form — stop once every field you can fill is filled, so a human can review it."
    )
```

- [ ] **Step 4: Run tests to green**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_fill_service.py -o addopts="" -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/integrations/form_fill_service.py apps/integrations/tests/test_form_fill_service.py
git commit -m "feat(integrations): claim→secrets/task builder (PII stays out of the prompt)"
```

---

## Task 6: The sidebar endpoints

Six `APIView` endpoints in a new module, authed with `ZendeskSidebarAuth`, wired into `__init__.py` + `urls.py`. They create/advance the `FormFill` row and call the wrapper (mocked in tests). Screenshots are proxied as data URLs so the sidebar never loads cross-origin images.

**Files:**
- Create: `apps/integrations/views/form_fill.py`
- Modify: `apps/integrations/views/__init__.py`, `apps/integrations/urls.py`
- Test: `apps/integrations/tests/test_form_fill_endpoints.py`

- [ ] **Step 1: Write the failing tests**

```python
# apps/integrations/tests/test_form_fill_endpoints.py
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
               return_value={'id': 'S1', 'live_url': 'https://live/s1'}) as m:
        resp = api.post(reverse('zd-form-fill-start'),
                        {'ticket_id': '55', 'url': 'https://lf.example/r', 'post_screenshot': True},
                        format='json', **_auth())
    assert resp.status_code == 200
    assert resp.data['session_id'] == 'S1'
    assert resp.data['live_url'] == 'https://live/s1'
    ff = FormFill.objects.get(id=resp.data['form_fill_id'])
    assert ff.status == FormFill.STATUS_STARTED
    assert ff.browser_use_session_id == 'S1'
    # the task passed to Browser Use must not contain the real name
    assert 'Jo' not in m.call_args[1]['task']


@pytest.mark.django_db
def test_submit_advances_and_optionally_posts_note(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    ff = FormFill.objects.create(claim=claim, form_url='https://lf.example/r',
                                 browser_use_session_id='S1', status=FormFill.STATUS_FILLED,
                                 posted_to_ticket=False)
    ff_post = FormFill.objects.create(claim=claim, form_url='https://lf.example/r',
                                      browser_use_session_id='S2', status=FormFill.STATUS_FILLED)
    with patch('apps.integrations.views.form_fill.browser_use.continue_session', return_value={'id': 'S1'}), \
         patch('apps.integrations.views.form_fill.browser_use.get_session',
               return_value={'status': 'idle', 'output': 'Submitted, ref 123'}), \
         patch('apps.integrations.views.form_fill.browser_use.latest_screenshot_url', return_value=''), \
         patch('apps.integrations.views.form_fill.post_zendesk_comment') as note:
        resp = api.post(reverse('zd-form-fill-submit'),
                        {'session_id': 'S1', 'ticket_id': '55', 'post_screenshot': False},
                        format='json', **_auth())
    assert resp.status_code == 200
    ff.refresh_from_db()
    assert ff.status == FormFill.STATUS_SUBMITTED
    assert note.called is False   # post_screenshot False → no note


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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_fill_endpoints.py -o addopts="" -q`
Expected: FAIL (`NoReverseMatch` / import errors).

- [ ] **Step 3: Implement the views**

```python
# apps/integrations/views/form_fill.py
"""Zendesk sidebar 'Form filling' endpoints: drive Browser Use to fill an
institution form from a claim, with a human approval gate before submit. Every
attempt is a FormFill row. Auth: ZendeskSidebarAuth (bearer token)."""
import logging

from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework import status

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.integrations import browser_use
from apps.integrations.form_fill_service import (
    build_form_secrets, build_fill_task, SUBMIT_TASK, form_host)
from apps.integrations.models import FormFill
from apps.integrations.services import post_zendesk_comment
from apps.integrations.views.auth import ZendeskSidebarAuth

logger = logging.getLogger(__name__)


def _claim_for(ticket_id):
    return Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None


class FormFillStartView(APIView):
    """POST /api/integrations/zd/form-fill/start
    Body: {ticket_id, url, post_screenshot?, image_ref?}
    Starts a Browser Use session that fills (but does not submit) the form."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-start')
        if auth_error:
            return auth_error

        if not SystemSettings.get_instance().form_filling_enabled:
            return Response({'error': 'Form filling is turned off in Settings.'},
                            status=status.HTTP_400_BAD_REQUEST)

        ticket_id = str(request.data.get('ticket_id', '')).strip()
        url = str(request.data.get('url', '')).strip()
        post_screenshot = bool(request.data.get('post_screenshot', False))
        if not url:
            return Response({'error': 'Paste the form URL first.'}, status=status.HTTP_400_BAD_REQUEST)

        claim = _claim_for(ticket_id)
        if not claim:
            return Response({'error': 'Link a LORA claim to this ticket to use form filling.'},
                            status=status.HTTP_400_BAD_REQUEST)

        host = form_host(url)
        secrets = build_form_secrets(claim, host)
        task = build_fill_task(url, secrets)

        ff = FormFill.objects.create(
            claim=claim, form_url=url, status=FormFill.STATUS_STARTED,
            created_by=request.user if request.user.is_authenticated else None,
            posted_to_ticket=False)
        # (image upload handled in Task 7; image_ref reserved here.)
        try:
            session = browser_use.create_session(task=task, secrets=secrets, allowed_domains=[host])
        except browser_use.BrowserUseError as e:
            ff.status = FormFill.STATUS_FAILED; ff.error = str(e); ff.save()
            return Response({'error': str(e), 'form_fill_id': ff.id},
                            status=status.HTTP_502_BAD_GATEWAY)

        ff.browser_use_session_id = session.get('id', '')
        ff.save(update_fields=['browser_use_session_id', 'updated_at'])
        return Response({'form_fill_id': ff.id, 'session_id': session.get('id', ''),
                         'live_url': session.get('live_url', ''), 'status': 'started',
                         'post_screenshot': post_screenshot}, status=status.HTTP_200_OK)


class FormFillStatusView(APIView):
    """POST {session_id} → {status, screenshot(dataURL|''), live_url}."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-status')
        if auth_error:
            return auth_error
        session_id = str(request.data.get('session_id', '')).strip()
        ff = FormFill.objects.filter(browser_use_session_id=session_id).first()
        try:
            st = browser_use.get_session(session_id)
        except browser_use.BrowserUseError as e:
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        bu_status = st.get('status', '')
        # idle/stopped after a fill task = ready for review
        if ff and ff.status == FormFill.STATUS_STARTED and bu_status in ('idle', 'stopped'):
            ff.status = FormFill.STATUS_FILLED; ff.filled_at = timezone.now()
            ff.result_output = str(st.get('output', ''))[:5000]
            ff.save(update_fields=['status', 'filled_at', 'result_output', 'updated_at'])
        screenshot = _proxy_screenshot(session_id)
        return Response({'status': ff.status if ff else bu_status, 'bu_status': bu_status,
                         'screenshot': screenshot}, status=status.HTTP_200_OK)


class FormFillSubmitView(APIView):
    """POST {session_id, ticket_id, post_screenshot} → continue the session to submit."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-submit')
        if auth_error:
            return auth_error
        session_id = str(request.data.get('session_id', '')).strip()
        ticket_id = str(request.data.get('ticket_id', '')).strip()
        post_screenshot = bool(request.data.get('post_screenshot', False))
        ff = FormFill.objects.filter(browser_use_session_id=session_id).first()
        try:
            browser_use.continue_session(session_id, task=SUBMIT_TASK)
            st = browser_use.get_session(session_id)
        except browser_use.BrowserUseError as e:
            if ff:
                ff.status = FormFill.STATUS_FAILED; ff.error = str(e); ff.save()
            return Response({'error': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        screenshot = _proxy_screenshot(session_id)
        if ff:
            ff.status = FormFill.STATUS_SUBMITTED; ff.submitted_at = timezone.now()
            ff.result_output = str(st.get('output', ''))[:5000]
            ff.save(update_fields=['status', 'submitted_at', 'result_output', 'updated_at'])

        if post_screenshot and screenshot and ticket_id:
            note = (f'<p>📝 <strong>Form filled &amp; submitted via LORA</strong></p>'
                    f'<p><img src="{screenshot}" alt="form submission confirmation" /></p>')
            try:
                post_zendesk_comment(ticket_id, comment_body='', is_internal=True, html_body=note)
                if ff:
                    ff.posted_to_ticket = True; ff.save(update_fields=['posted_to_ticket', 'updated_at'])
            except Exception as e:
                logger.warning('Form-fill note post failed for ticket %s: %s', ticket_id, e)

        return Response({'status': 'submitted', 'screenshot': screenshot}, status=status.HTTP_200_OK)


class FormFillCancelView(APIView):
    """POST {session_id} → stop the session, mark the FormFill cancelled."""
    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='form-fill-cancel')
        if auth_error:
            return auth_error
        session_id = str(request.data.get('session_id', '')).strip()
        ff = FormFill.objects.filter(browser_use_session_id=session_id).first()
        try:
            browser_use.stop_session(session_id)
        except browser_use.BrowserUseError as e:
            logger.warning('Form-fill cancel stop failed: %s', e)
        if ff and ff.status not in (FormFill.STATUS_SUBMITTED,):
            ff.status = FormFill.STATUS_CANCELLED; ff.save(update_fields=['status', 'updated_at'])
        return Response({'status': 'cancelled'}, status=status.HTTP_200_OK)


def _proxy_screenshot(session_id: str) -> str:
    """Fetch the latest screenshot from Browser Use and return it as a data: URL so
    the sidebar loads it same-origin (no CSP/whitelist change). '' if none."""
    import base64
    import requests
    try:
        src = browser_use.latest_screenshot_url(session_id)
        if not src:
            return ''
        r = requests.get(src, timeout=30)
        if r.status_code >= 400:
            return ''
        ctype = r.headers.get('Content-Type', 'image/png')
        b64 = base64.b64encode(r.content).decode()
        return f'data:{ctype};base64,{b64}'
    except Exception as e:
        logger.warning('Screenshot proxy failed: %s', e)
        return ''
```

- [ ] **Step 4: Wire re-exports**

In `apps/integrations/views/__init__.py`, add the import and `__all__` entries:

```python
from apps.integrations.views.form_fill import (
    FormFillStartView, FormFillStatusView, FormFillSubmitView, FormFillCancelView,
)
```
Add `'FormFillStartView', 'FormFillStatusView', 'FormFillSubmitView', 'FormFillCancelView'` to `__all__`.

- [ ] **Step 5: Wire URLs**

In `apps/integrations/urls.py`, add the imports to the `from apps.integrations.views import (...)` block and these routes to `urlpatterns`:

```python
    path('zd/form-fill/start/', FormFillStartView.as_view(), name='zd-form-fill-start'),
    path('zd/form-fill/status/', FormFillStatusView.as_view(), name='zd-form-fill-status'),
    path('zd/form-fill/submit/', FormFillSubmitView.as_view(), name='zd-form-fill-submit'),
    path('zd/form-fill/cancel/', FormFillCancelView.as_view(), name='zd-form-fill-cancel'),
```

- [ ] **Step 6: Run tests to green**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_fill_endpoints.py -o addopts="" -q`
Expected: PASS (7 tests).

- [ ] **Step 7: Commit**

```bash
git add apps/integrations/views/form_fill.py apps/integrations/views/__init__.py apps/integrations/urls.py apps/integrations/tests/test_form_fill_endpoints.py
git commit -m "feat(integrations): form-fill sidebar endpoints (start/status/submit/cancel)"
```

---

## Task 7: Image attachments (both sources)

Adds the two image endpoints and finishes `browser_use.upload_file` using the path confirmed in Task 1. Source A lists + downloads a ticket attachment; source B accepts a multipart upload stored on the `FormFill`.

**Files:**
- Modify: `apps/integrations/browser_use.py` (implement `upload_file`)
- Modify: `apps/integrations/views/form_fill.py` (attachments + upload views; pass image into `create_session`)
- Modify: `apps/integrations/views/__init__.py`, `apps/integrations/urls.py`
- Modify: `apps/integrations/services.py` (add `fetch_zendesk_attachment(content_url) -> (bytes, content_type)` if not present)
- Test: extend `apps/integrations/tests/test_form_fill_endpoints.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to apps/integrations/tests/test_form_fill_endpoints.py
import io

@pytest.mark.django_db
def test_attachments_lists_ticket_images(api, settings_obj):
    Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    fake_comments = [{'attachments': [
        {'file_name': 'item.jpg', 'content_type': 'image/jpeg', 'content_url': 'https://zd/att/1'},
        {'file_name': 'note.pdf', 'content_type': 'application/pdf', 'content_url': 'https://zd/att/2'},
    ]}]
    with patch('apps.integrations.views.form_fill.fetch_zendesk_comments', return_value=fake_comments):
        resp = api.post(reverse('zd-form-fill-attachments'), {'ticket_id': '55'}, format='json', **_auth())
    assert resp.status_code == 200
    names = [a['filename'] for a in resp.data['attachments']]
    assert 'item.jpg' in names and 'note.pdf' not in names   # images only


@pytest.mark.django_db
def test_upload_image_stores_on_formfill(api, settings_obj):
    claim = Claim.objects.create(client_email='c@e.com', zd_ticket_id='55', alf_claim_id='ALF1')
    img = io.BytesIO(b'\xff\xd8\xff\xe0fakejpeg'); img.name = 'p.jpg'
    resp = api.post(reverse('zd-form-fill-upload'),
                    {'ticket_id': '55', 'image': img}, format='multipart', **_auth())
    assert resp.status_code == 200
    ff = FormFill.objects.get(id=resp.data['form_fill_id'])
    assert ff.image_source == FormFill.IMAGE_SOURCE_UPLOAD
    assert ff.image_name == 'p.jpg'
```

- [ ] **Step 2: Run and watch fail**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_fill_endpoints.py -o addopts="" -q -k "attachments or upload_image"`
Expected: FAIL (NoReverseMatch).

- [ ] **Step 3: Implement `upload_file` in the wrapper**

Implement `apps/integrations/browser_use.upload_file(...)` using the Task-1-confirmed path (v3 workspace upload or v2 presigned session file). Return the file reference/name the agent uses. Keep it the only place that knows the upload mechanics.

- [ ] **Step 4: Add `fetch_zendesk_attachment` to services**

In `apps/integrations/services.py`:

```python
def fetch_zendesk_attachment(content_url: str, *, timeout: int = 30):
    """Download a Zendesk attachment's bytes. Returns (bytes, content_type)."""
    import urllib.request
    headers = _get_zendesk_auth_headers()
    req = urllib.request.Request(content_url, headers=headers, method='GET')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get('Content-Type', 'application/octet-stream')
```

- [ ] **Step 5: Add the two views + extend start**

In `apps/integrations/views/form_fill.py` add `FormFillAttachmentsView` (lists image attachments via `fetch_zendesk_comments`, filtering `content_type` startswith `image/`) and `FormFillUploadView` (accepts a multipart `image`, validates size ≤10 MB and an image content type, creates/updates a `FormFill` with `image_source=UPLOAD`, `image`, `image_name`, returns `form_fill_id`). Extend `FormFillStartView` to accept `image_ref` (a `form_fill_id` from upload, or a ticket attachment URL), upload it via `browser_use.upload_file`, append "upload the provided image into the form's photo field" to the task, and record `image_source` on the row. Import `fetch_zendesk_comments`, `fetch_zendesk_attachment` at top.

- [ ] **Step 6: Wire re-exports + URLs**

Add `FormFillAttachmentsView`, `FormFillUploadView` to `__init__.py` `__all__`/imports and to `urls.py`:

```python
    path('zd/form-fill/attachments/', FormFillAttachmentsView.as_view(), name='zd-form-fill-attachments'),
    path('zd/form-fill/upload/', FormFillUploadView.as_view(), name='zd-form-fill-upload'),
```

- [ ] **Step 7: Run tests to green**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_fill_endpoints.py -o addopts="" -q`
Expected: PASS (all, incl. the 2 new).

- [ ] **Step 8: Commit**

```bash
git add apps/integrations/ docs/
git commit -m "feat(integrations): form-fill image attachments (ticket pick + agent upload)"
```

---

## Task 8: Per-claim history panel on the claim page

Surface the `FormFill` history on the LORA claim detail page (read-only).

**Files:**
- Modify: the claim-detail view (`agent_claim_detail` in `apps/users/views.py`) — add `form_fills` to context.
- Create: `templates/manager/_claim_form_fills.html` — a small panel.
- Modify: the claim-detail template to `{% include %}` it.
- Test: `apps/users/tests/test_views.py` (add a case).

- [ ] **Step 1: Write the failing test**

```python
# add to apps/users/tests/test_views.py
@pytest.mark.django_db
def test_claim_detail_lists_form_fills():
    from apps.integrations.models import FormFill
    from django.contrib.auth import get_user_model
    User = get_user_model()
    User.objects.create_user(username='ff_view', password='x')
    claim = Claim.objects.create(client_email='c@e.com', alf_claim_id='ALFX', zd_ticket_id='77')
    FormFill.objects.create(claim=claim, form_url='https://lf.example/r',
                            status=FormFill.STATUS_SUBMITTED)
    c = Client(); c.login(username='ff_view', password='x')
    resp = c.get(f'/agent/claims/{claim.id}/')
    assert resp.status_code == 200
    assert 'lf.example' in resp.content.decode()
```

- [ ] **Step 2: Run and watch fail**

Run: `.venv/bin/python -m pytest "apps/users/tests/test_views.py::test_claim_detail_lists_form_fills" -o addopts="" -q`
Expected: FAIL (URL not in content).

- [ ] **Step 3: Add to the view context**

In `agent_claim_detail` (apps/users/views.py), add `'form_fills': claim.form_fills.all()[:10]` to the render context.

- [ ] **Step 4: Create the panel**

```html
<!-- templates/manager/_claim_form_fills.html -->
{% if form_fills %}
<div class="overflow-hidden lora-card">
  <div class="border-b border-gray-200 px-5 py-3"><h3 class="lora-card-title">Form fills</h3></div>
  <ul role="list" class="divide-y divide-gray-200">
    {% for ff in form_fills %}
    <li class="px-5 py-3 text-sm">
      <div class="flex items-center justify-between gap-3">
        <span class="truncate text-gray-900">{{ ff.form_url }}</span>
        <span class="shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset
          {% if ff.status == 'SUBMITTED' %}bg-green-50 text-green-700 ring-green-600/20
          {% elif ff.status == 'FAILED' %}bg-red-50 text-red-700 ring-red-600/10
          {% elif ff.status == 'CANCELLED' %}bg-gray-50 text-gray-600 ring-gray-500/10
          {% else %}bg-yellow-50 text-yellow-800 ring-yellow-600/20{% endif %}">{{ ff.get_status_display }}</span>
      </div>
      <div class="lora-meta mt-0.5">{{ ff.created_at }}{% if ff.created_by %} · {{ ff.created_by }}{% endif %}</div>
    </li>
    {% endfor %}
  </ul>
</div>
{% endif %}
```

- [ ] **Step 5: Include it on the claim page**

In the claim-detail template, add `{% include 'manager/_claim_form_fills.html' %}` in the sidebar/secondary column.

- [ ] **Step 6: Run tests to green**

Run: `.venv/bin/python -m pytest "apps/users/tests/test_views.py::test_claim_detail_lists_form_fills" -o addopts="" -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/users/views.py templates/manager/ apps/users/tests/test_views.py
git commit -m "feat(claims): show per-claim form-fill history on the claim page"
```

---

## Task 9: The Zendesk app "Form filling" tab

The sidebar UI. No automated tests (it's the app shell, tested manually via `zcli apps:server`). Prod CSP forbids eval/Alpine → vanilla JS + `onclick`, dynamic content via `innerHTML` with `escapeHtml`.

**Files:**
- Modify: `zendesk_app/assets/iframe.html`
- Modify: `zendesk_app/assets/app.js`

- [ ] **Step 1: Add the tab + panel (iframe.html)**

Add to `<div id="tabs">`:

```html
    <button id="tab-formfill" class="tab" data-tab="formfill">Form filling</button>
```

Add the panel (after the others):

```html
  <section id="panel-formfill" class="panel" hidden>
    <p class="muted">Paste a lost-item report form's web address. LORA fills it from this
      claim, you review the screenshot, then approve to submit. Nothing is submitted
      without you.</p>
    <input id="ff-url" type="url" placeholder="https://…/report" autocomplete="off"
           style="width:100%;box-sizing:border-box;margin:6px 0">
    <div id="ff-image-row" class="muted" style="font-size:12px"></div>
    <label style="display:flex;gap:6px;align-items:center;font-size:12px;margin:6px 0">
      <input id="ff-post" type="checkbox"> Post the confirmation screenshot to the ticket
    </label>
    <div class="actions">
      <button id="ff-fill" type="button" class="wide">Fill form</button>
    </div>
    <div id="ff-status"></div>
    <div id="ff-shot"></div>
    <div id="ff-actions" hidden>
      <button id="ff-approve" type="button">Approve &amp; submit</button>
      <button id="ff-live" type="button">Open live view</button>
      <button id="ff-cancel" type="button">Cancel</button>
    </div>
    <div id="ff-history"></div>
  </section>
```

- [ ] **Step 2: Add the tab-switch hook**

In the `.tab` click handler in app.js add the panel toggle + history load:

```javascript
    document.getElementById('panel-formfill').hidden = which !== 'formfill';
    if (which === 'formfill') loadFormFills();
```

- [ ] **Step 3: Add the form-fill JS**

```javascript
// --- form filling ---
let ffSession = null, ffLiveUrl = null, ffPoll = null;

async function loadFormFills() {
  // optional: list-by-claim could be a future endpoint; v1 leaves history to the LORA claim page.
  const row = document.getElementById('ff-image-row');
  try {
    const d0 = await client.get(['ticket.id']);
    const resp = await loraRequest('/api/integrations/zd/form-fill/attachments/',
      { ticket_id: String(d0['ticket.id']) });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    const atts = data.attachments || [];
    if (atts.length) {
      row.innerHTML = 'Image (optional): '
        + '<select id="ff-att"><option value="">— none —</option>'
        + atts.map(a => `<option value="${escapeHtml(a.url)}">${escapeHtml(a.filename)}</option>`).join('')
        + '</select> or <input id="ff-file" type="file" accept="image/*">';
    } else {
      row.innerHTML = 'Image (optional): <input id="ff-file" type="file" accept="image/*">';
    }
  } catch (e) { row.textContent = ''; }
}

async function ffStartFill() {
  const url = document.getElementById('ff-url').value.trim();
  const statusEl = document.getElementById('ff-status');
  if (!url) { statusEl.textContent = 'Paste the form URL first.'; return; }
  document.getElementById('ff-fill').disabled = true;
  statusEl.textContent = 'Starting…';
  try {
    const d0 = await client.get(['ticket.id']);
    const body = { ticket_id: String(d0['ticket.id']), url: url,
                   post_screenshot: document.getElementById('ff-post').checked };
    const att = document.getElementById('ff-att');
    if (att && att.value) body.image_ref = att.value;
    const resp = await loraRequest('/api/integrations/zd/form-fill/start/', body);
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    if (data.error) { statusEl.textContent = data.error; document.getElementById('ff-fill').disabled = false; return; }
    ffSession = data.session_id; ffLiveUrl = data.live_url;
    statusEl.textContent = 'Filling the form… (you can Open live view to watch)';
    document.getElementById('ff-actions').hidden = false;
    ffPoll = setInterval(ffCheck, 4000);
  } catch (e) {
    statusEl.innerHTML = '<span class="error">' + escapeHtml(diagnose(e)) + '</span>';
    document.getElementById('ff-fill').disabled = false;
  }
}

async function ffCheck() {
  if (!ffSession) return;
  try {
    const resp = await loraRequest('/api/integrations/zd/form-fill/status/', { session_id: ffSession });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    if (data.screenshot) {
      document.getElementById('ff-shot').innerHTML =
        `<img src="${data.screenshot}" alt="filled form" style="width:100%;border:1px solid #e5e7eb;border-radius:8px">`;
    }
    if (data.status === 'FILLED') {
      clearInterval(ffPoll); ffPoll = null;
      document.getElementById('ff-status').textContent = 'Filled — review and approve to submit.';
    }
  } catch (e) { /* keep polling */ }
}

async function ffApprove() {
  if (!ffSession) return;
  const statusEl = document.getElementById('ff-status');
  document.getElementById('ff-approve').disabled = true;
  statusEl.textContent = 'Submitting…';
  try {
    const d0 = await client.get(['ticket.id']);
    const resp = await loraRequest('/api/integrations/zd/form-fill/submit/',
      { session_id: ffSession, ticket_id: String(d0['ticket.id']),
        post_screenshot: document.getElementById('ff-post').checked });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    if (data.screenshot) {
      document.getElementById('ff-shot').innerHTML =
        `<img src="${data.screenshot}" alt="confirmation" style="width:100%;border:1px solid #e5e7eb;border-radius:8px">`;
    }
    statusEl.textContent = data.error ? data.error : '✓ Submitted.';
    document.getElementById('ff-actions').hidden = true;
    ffSession = null;
  } catch (e) {
    statusEl.innerHTML = '<span class="error">' + escapeHtml(diagnose(e)) + '</span>';
    document.getElementById('ff-approve').disabled = false;
  }
}

async function ffCancel() {
  if (ffPoll) { clearInterval(ffPoll); ffPoll = null; }
  if (ffSession) {
    try { await loraRequest('/api/integrations/zd/form-fill/cancel/', { session_id: ffSession }); } catch (e) {}
  }
  ffSession = null;
  document.getElementById('ff-actions').hidden = true;
  document.getElementById('ff-status').textContent = 'Cancelled.';
}

document.getElementById('ff-fill').onclick = ffStartFill;
document.getElementById('ff-approve').onclick = ffApprove;
document.getElementById('ff-cancel').onclick = ffCancel;
document.getElementById('ff-live').onclick = () => { if (ffLiveUrl) window.open(ffLiveUrl, '_blank'); };
```

(Image upload via `ff-file` posts to `/zd/form-fill/upload/` first to get a `form_fill_id`/ref, then passes it as `image_ref` — wire this in when finishing the file path from Task 7; for the first pass the attachment-`<select>` path is enough.)

- [ ] **Step 4: Manual smoke test (with the flag ON + a live key)**

Run the app locally against LORA: `cd zendesk_app && zcli apps:server`, open a real claim-backed ticket with `?zcli_apps=true`, set the form URL to a known lost-item form, click **Fill form**, watch via **Open live view**, **Approve & submit**, confirm the screenshot + (if ticked) the ticket note. Confirm a `FormFill` row exists with the right status.

- [ ] **Step 5: Commit**

```bash
git add zendesk_app/assets/iframe.html zendesk_app/assets/app.js
git commit -m "feat(zendesk-app): Form filling tab (fill, review, approve & submit)"
```

> **Deploy:** backend ships via Railway (git push). The sidebar app goes live only after the user runs `zcli apps:update` from `zendesk_app/`.

---

## Self-Review

**Spec coverage:**
- Form filling tab → Task 9. Auto-fill from claim → Task 5. Fill→approve→submit → Tasks 6 (submit via follow-up). Cloud + secrets (PII off the LLM) → Tasks 4/5. Screenshot review in tab + pop-out live view → Tasks 6 (`_proxy_screenshot`) + 9 (`ff-live`). Off-by-default flag + key → Task 2. Images (both sources) → Task 7. FormFill audit + per-claim history → Tasks 3 + 8. Screenshot→note (reuse #85 `html_body`) → Task 6 submit. ✅ all covered.
- Open questions from the spec are pinned in **Task 1** (screenshot path, file-upload path, secrets shape, follow-up mechanism) and the 15-min window (v1: re-run on lapse — surfaced by the status call; keep-alive is a future optimization, noted).

**Placeholder scan:** `upload_file` is intentionally deferred to Task 1/7 (flagged, not silent). Task 7 Steps 3/5 describe view edits in prose rather than full code — acceptable as they mirror the fully-coded patterns in Task 6; if executing out of order, read Task 6 first.

**Type/name consistency:** `FormFill.STATUS_*`, `IMAGE_SOURCE_*`, `browser_use_session_id`, `browser_use.create_session/continue_session/get_session/latest_screenshot_url/stop_session/upload_file`, `build_form_secrets/build_fill_task/SUBMIT_TASK/form_host`, route names `zd-form-fill-{start,status,submit,cancel,attachments,upload}`, settings `browser_use_api_key/browser_use_model/form_filling_enabled` — used identically across tasks. ✅

**Net new dependency:** none — uses `requests` (already used by the Anthropic path) and existing patterns.
