# Zendesk Agent Sidebar App — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Zendesk ticket-sidebar app that shows an AI briefing + key facts and an ask-the-AI chat (scoped to the current claim), backed by two new LORA endpoints that enrich ticket content with LORA's own data, run it through the PII-safe `AIClient`, and return results.

**Architecture:** LORA = secure AI gateway + cross-system data; the Zendesk app = UI + ticket-data source. The app reads the ticket locally and sends content to LORA via Zendesk's proxied `client.request()` (secret stays server-side, no CORS). Two new sidebar-authed endpoints (`POST /zd/briefing/`, `POST /zd/chat/`) reuse `AgentChatService`, `AIClient`, and the existing claim/enrichment data. Action buttons (browser-use, etc.) are out of scope but the pattern extends to them.

**Tech Stack:** Django 5.2 + DRF, `apps/ai/` (AIClient + Pydantic schemas), Zendesk Apps Framework (ZAF) v2 + `zcli`, pytest.

**Spec:** [docs/superpowers/specs/2026-06-10-zendesk-agent-sidebar-app-design.md](../specs/2026-06-10-zendesk-agent-sidebar-app-design.md)

---

## File Structure

**Backend (modified/created):**
- `apps/ai/schemas.py` — add `BriefingSummary` schema
- `apps/ai/tests/test_schemas.py` — add schema tests
- `apps/integrations/services.py` — add `build_claim_facts(claim)` helper
- `apps/integrations/views.py` — add `ZendeskBriefingView`, `ZendeskChatView`
- `apps/integrations/urls.py` — wire the two new routes
- `apps/integrations/tests/test_sidebar_ai_endpoints.py` — new test file for both endpoints

**Frontend (new — a self-contained ZAF app, NOT a Django app):**
- `zendesk_app/manifest.json`
- `zendesk_app/assets/iframe.html`
- `zendesk_app/assets/app.js`
- `zendesk_app/assets/styles.css`
- `zendesk_app/assets/logo.png` (placeholder)
- `zendesk_app/translations/en.json`
- `zendesk_app/README.md` — zcli dev/package/install steps

**Auth note (applies to both new endpoints):** authenticate with `ZendeskSidebarAuth.authenticate(request)` — it reads the `Authorization` header (accepts `Bearer <token>` or the raw token) and compares against `SystemSettings.sidebar_secret_token` with `hmac.compare_digest`. Tests pass `HTTP_AUTHORIZATION='Bearer <token>'`.

---

## PHASE A — Backend

### Task 1: Add `BriefingSummary` schema

**Files:**
- Modify: `apps/ai/schemas.py`
- Modify: `apps/ai/tests/test_schemas.py`

- [ ] **Step 1: Add the failing test**

Append to `apps/ai/tests/test_schemas.py`:

```python
from apps.ai.schemas import BriefingSummary


def test_briefing_summary_accepts_valid_payload():
    obj = BriefingSummary.model_validate({
        "summary": "Bag lost on UA123; searching 9 days.",
        "next_steps": ["Chase airport", "Send 11-day update"],
    })
    assert obj.summary.startswith("Bag lost")
    assert len(obj.next_steps) == 2


def test_briefing_summary_next_steps_defaults_empty():
    obj = BriefingSummary.model_validate({"summary": "All quiet."})
    assert obj.next_steps == []


def test_briefing_summary_rejects_too_long_summary():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BriefingSummary.model_validate({"summary": "x" * 601})


def test_briefing_summary_caps_next_steps_count():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BriefingSummary.model_validate({
            "summary": "ok",
            "next_steps": [f"step {i}" for i in range(7)],
        })
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.venv/bin/pytest apps/ai/tests/test_schemas.py -k briefing -v`
Expected: ImportError (BriefingSummary not defined).

- [ ] **Step 3: Implement**

Append to `apps/ai/schemas.py`:

```python
class BriefingSummary(BaseModel):
    """Schema for the Zendesk sidebar briefing (POST /zd/briefing/).
    The LLM produces a short summary + a few suggested next steps. The
    structured `facts` block is assembled by the view, not the LLM, so it is
    not part of this schema."""

    summary: str = Field(max_length=600)
    next_steps: list[str] = Field(default_factory=list, max_length=6)
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest apps/ai/tests/test_schemas.py -k briefing -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/ai/schemas.py apps/ai/tests/test_schemas.py
git commit -m "feat(ai): add BriefingSummary schema for the Zendesk sidebar briefing"
```

---

### Task 2: `build_claim_facts(claim)` enrichment helper

**Files:**
- Modify: `apps/integrations/services.py`
- Modify: `apps/integrations/tests/test_zendesk_services.py`

- [ ] **Step 1: Add the failing test**

Append to `apps/integrations/tests/test_zendesk_services.py`:

```python
@pytest.mark.django_db
def test_build_claim_facts_returns_panel_facts():
    from apps.integrations.services import build_claim_facts
    from apps.claims.models import Claim
    from apps.communications.models import EmailLog
    from datetime import date

    claim = Claim.objects.create(
        alf_claim_id='ALF7000001', zd_ticket_id='70001',
        client_email='c@example.com', status='Searching',
        deadline_date=date(2026, 7, 1),
    )
    EmailLog.objects.create(claim=claim, subject='a', body='', category='UNKNOWN',
                            action_required=True, auto_resolved=False)
    EmailLog.objects.create(claim=claim, subject='b', body='', category='OBJECT_FOUND',
                            action_required=False, auto_resolved=True)

    facts = build_claim_facts(claim)
    assert facts['status'] == 'Searching'
    assert facts['deadline'] == '2026-07-01'
    assert facts['emails_total'] == 2
    assert facts['emails_unresolved'] == 1
    assert facts['disputes_total'] == 0
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.venv/bin/pytest apps/integrations/tests/test_zendesk_services.py -k build_claim_facts -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Add to `apps/integrations/services.py`:

```python
def build_claim_facts(claim) -> dict:
    """Compact, panel-ready facts for the Zendesk sidebar Briefing tab.
    Uses only LORA-side data the Zendesk ticket does not already have.
    `disputes_total` is a count (no dependence on the Dispute status enum)."""
    from apps.payments.models import Dispute

    emails = claim.emails.all()
    return {
        'status': claim.get_status_display(),
        'deadline': claim.deadline_date.isoformat() if claim.deadline_date else None,
        'emails_total': emails.count(),
        'emails_unresolved': emails.filter(action_required=True, auto_resolved=False).count(),
        'disputes_total': Dispute.objects.filter(claim=claim).count(),
    }
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/pytest apps/integrations/tests/test_zendesk_services.py -k build_claim_facts -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/integrations/services.py apps/integrations/tests/test_zendesk_services.py
git commit -m "feat(integrations): add build_claim_facts helper for sidebar briefing"
```

---

### Task 3: `ZendeskBriefingView` — `POST /zd/briefing/`

**Files:**
- Modify: `apps/integrations/views.py`
- Create: `apps/integrations/tests/test_sidebar_ai_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/integrations/tests/test_sidebar_ai_endpoints.py`:

```python
import pytest
from unittest.mock import patch, MagicMock
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.config.models import SystemSettings

SECRET = 'sidebar-secret-xyz'


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def settings_obj(db):
    ss, _ = SystemSettings.objects.get_or_create(pk=1, defaults={})
    ss.sidebar_secret_token = SECRET
    ss.ai_api_key = 'test'
    ss.ai_api_base = 'https://api.example.com/v1'
    ss.ai_api_model = 'test-model'
    ss.pii_tokenization_salt = 'salt-long-enough-for-real-use'
    ss.save()
    return ss


def _briefing_body(ticket_id='70001'):
    return {
        'ticket_id': ticket_id,
        'requester_email': 'c@example.com',
        'subject': 'Lost item ALF7000001',
        'description': 'I lost my black bag on UA123',
        'comments': ['Airline says not located yet'],
    }


@pytest.mark.django_db
def test_briefing_requires_auth(api_client, settings_obj):
    resp = api_client.post(reverse('zendesk-sidebar-briefing'),
                           data=_briefing_body(), format='json')
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_briefing_returns_summary_next_steps_and_facts(api_client, settings_obj):
    Claim.objects.create(alf_claim_id='ALF7000001', zd_ticket_id='70001',
                         client_email='c@example.com', status='Searching')

    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                '{"summary": "Bag lost on UA123, searching.", '
                '"next_steps": ["Chase airport"]}'
            )))],
        )
        resp = api_client.post(
            reverse('zendesk-sidebar-briefing'), data=_briefing_body(), format='json',
            HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )

    assert resp.status_code == 200
    assert 'Bag lost' in resp.data['summary']
    assert resp.data['next_steps'] == ['Chase airport']
    assert resp.data['facts']['status'] == 'Searching'


@pytest.mark.django_db
def test_briefing_graceful_when_no_claim(api_client, settings_obj):
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=(
                '{"summary": "No linked claim; based on ticket only.", "next_steps": []}'
            )))],
        )
        resp = api_client.post(
            reverse('zendesk-sidebar-briefing'),
            data=_briefing_body(ticket_id='99999'), format='json',
            HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )
    assert resp.status_code == 200
    assert resp.data['facts'] == {}  # no claim -> empty facts


@pytest.mark.django_db
def test_briefing_tokenizes_pii_before_ai(api_client, settings_obj):
    Claim.objects.create(alf_claim_id='ALF7000001', zd_ticket_id='70001',
                         client_email='c@example.com', status='Searching')
    with patch('apps.ai.client.OpenAI') as MockOpenAI:
        MockOpenAI.return_value.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content='{"summary":"s","next_steps":[]}'))],
        )
        body = _briefing_body()
        body['description'] = 'Reach me at alice@example.com'
        api_client.post(reverse('zendesk-sidebar-briefing'), data=body, format='json',
                        HTTP_AUTHORIZATION=f'Bearer {SECRET}')
        sent = MockOpenAI.return_value.chat.completions.create.call_args.kwargs['messages']
        user_content = sent[1]['content']
        assert 'alice@example.com' not in user_content
        assert '<EMAIL_' in user_content
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.venv/bin/pytest apps/integrations/tests/test_sidebar_ai_endpoints.py -k briefing -v`
Expected: errors (no `zendesk-sidebar-briefing` route / view).

- [ ] **Step 3: Implement the view**

Add to `apps/integrations/views.py` (near the other sidebar views). Imports `AIClient`, `BriefingSummary`, `build_claim_facts`, `ZendeskSidebarAuth`, `AIResponseValidationError` are at module level or local:

```python
class ZendeskBriefingView(APIView):
    """POST /api/integrations/zd/briefing/
    Body: {ticket_id, requester_email, subject, description, comments[]}
    Returns: {summary, next_steps[], facts{}} — AI briefing + LORA facts.
    Auth: ZendeskSidebarAuth (sidebar_secret_token)."""

    permission_classes = [AllowAny]

    BRIEFING_PROMPT = (
        "You are briefing a lost-item recovery agent who is about to handle a "
        "ticket. Using ONLY the provided ticket content and claim facts, write a "
        "2-3 sentence summary of where this claim stands, then list up to 4 "
        "concrete next steps the agent should take. Respond as JSON: "
        '{"summary": "...", "next_steps": ["..."]}.'
    )

    def post(self, request):
        if not ZendeskSidebarAuth.authenticate(request):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

        from apps.ai.client import AIClient
        from apps.ai.schemas import BriefingSummary
        from apps.ai.exceptions import AIResponseValidationError
        from apps.claims.models import Claim
        from apps.integrations.services import build_claim_facts

        data = request.data
        ticket_id = str(data.get('ticket_id', '')).strip()
        subject = str(data.get('subject', ''))
        description = str(data.get('description', ''))
        comments = data.get('comments') or []
        if not isinstance(comments, list):
            comments = [str(comments)]
        comments = [str(c)[:1000] for c in comments[:10]]

        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None
        facts = build_claim_facts(claim) if claim else {}

        trusted = {'claim_facts': str(facts)} if facts else None
        untrusted = {'ticket_subject': subject[:200], 'ticket_description': description[:2000]}
        if comments:
            untrusted['zendesk_comment'] = comments

        known_aliases = []
        if claim:
            # the per-case alias lives in the Zendesk custom field; if present on
            # the claim's emails it's already tokenized elsewhere. Pass client_email
            # is NOT an alias; leave aliases empty unless you store it on the claim.
            pass

        try:
            result = AIClient.complete(
                system_prompt=self.BRIEFING_PROMPT,
                trusted=trusted,
                untrusted=untrusted,
                known_pii={'aliases': known_aliases},
                response_schema=BriefingSummary,
                call_site='zendesk_briefing',
                temperature=0.4,
                max_tokens=500,
            )
        except AIResponseValidationError as e:
            logger.warning(f"Briefing AI validation failed for ticket {ticket_id}: {e}")
            return Response(
                {'summary': 'Briefing unavailable right now. Please use the Chat tab or retry.',
                 'next_steps': [], 'facts': facts},
                status=status.HTTP_200_OK,
            )

        return Response(
            {'summary': result.summary, 'next_steps': result.next_steps, 'facts': facts},
            status=status.HTTP_200_OK,
        )
```

- [ ] **Step 4: Add the URL (so reverse() resolves)**

In `apps/integrations/urls.py`, import `ZendeskBriefingView` and add:
```python
    path('zd/briefing/', ZendeskBriefingView.as_view(), name='zendesk-sidebar-briefing'),
```

- [ ] **Step 5: Run — expect PASS**

Run: `.venv/bin/pytest apps/integrations/tests/test_sidebar_ai_endpoints.py -k briefing -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/integrations/views.py apps/integrations/urls.py apps/integrations/tests/test_sidebar_ai_endpoints.py
git commit -m "feat(integrations): add Zendesk sidebar briefing endpoint (AI summary + facts)"
```

---

### Task 4: `ZendeskChatView` — `POST /zd/chat/`

**Files:**
- Modify: `apps/integrations/views.py`
- Modify: `apps/integrations/urls.py`
- Modify: `apps/integrations/tests/test_sidebar_ai_endpoints.py`

- [ ] **Step 1: Append failing tests**

Append to `apps/integrations/tests/test_sidebar_ai_endpoints.py`:

```python
@pytest.mark.django_db
def test_chat_requires_auth(api_client, settings_obj):
    resp = api_client.post(reverse('zendesk-sidebar-chat'),
                           data={'ticket_id': '70001', 'message': 'status?'}, format='json')
    assert resp.status_code in (401, 403)


@pytest.mark.django_db
def test_chat_answers_scoped_to_claim(api_client, settings_obj):
    Claim.objects.create(alf_claim_id='ALF7000001', zd_ticket_id='70001',
                         client_email='c@example.com', status='Searching')

    with patch('apps.agent.services.AgentChatService.process_message') as mock_pm:
        mock_pm.return_value = MagicMock(answer='Status is Searching.', sources=['claim'])
        resp = api_client.post(
            reverse('zendesk-sidebar-chat'),
            data={'ticket_id': '70001', 'message': 'what is the status?', 'history': []},
            format='json', HTTP_AUTHORIZATION=f'Bearer {SECRET}',
        )

    assert resp.status_code == 200
    assert resp.data['answer'] == 'Status is Searching.'
    # claim-scoped: process_message called with the resolved claim's id
    kwargs = mock_pm.call_args.kwargs
    assert kwargs.get('claim_ids') == ['ALF7000001']


@pytest.mark.django_db
def test_chat_no_claim_returns_friendly_message(api_client, settings_obj):
    resp = api_client.post(
        reverse('zendesk-sidebar-chat'),
        data={'ticket_id': '88888', 'message': 'status?'}, format='json',
        HTTP_AUTHORIZATION=f'Bearer {SECRET}',
    )
    assert resp.status_code == 200
    assert 'no lora claim' in resp.data['answer'].lower()
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.venv/bin/pytest apps/integrations/tests/test_sidebar_ai_endpoints.py -k chat -v`
Expected: errors (no route/view).

- [ ] **Step 3: Implement the view**

Add to `apps/integrations/views.py`:

```python
class ZendeskChatView(APIView):
    """POST /api/integrations/zd/chat/
    Body: {ticket_id, message, history[]}
    Returns: {answer, sources[]} — AI chat scoped to the ticket's claim.
    Auth: ZendeskSidebarAuth (sidebar_secret_token)."""

    permission_classes = [AllowAny]

    def post(self, request):
        if not ZendeskSidebarAuth.authenticate(request):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_401_UNAUTHORIZED)

        from apps.claims.models import Claim
        from apps.agent.services import AgentChatService

        data = request.data
        ticket_id = str(data.get('ticket_id', '')).strip()
        message = str(data.get('message', '')).strip()
        history = data.get('history') or []

        if not message:
            return Response({'error': 'message is required'}, status=status.HTTP_400_BAD_REQUEST)

        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None
        if not claim:
            return Response(
                {'answer': 'No LORA claim is linked to this ticket yet, so I cannot answer '
                           'claim-specific questions here.', 'sources': []},
                status=status.HTTP_200_OK,
            )

        result = AgentChatService().process_message(
            message=message,
            claim_ids=[claim.alf_claim_id],   # locks the chat to THIS claim
            conversation_history=history,
        )
        return Response({'answer': result.answer, 'sources': getattr(result, 'sources', [])},
                        status=status.HTTP_200_OK)
```

- [ ] **Step 4: Add the URL**

In `apps/integrations/urls.py`, import `ZendeskChatView` and add:
```python
    path('zd/chat/', ZendeskChatView.as_view(), name='zendesk-sidebar-chat'),
```

- [ ] **Step 5: Run — expect PASS**

Run: `.venv/bin/pytest apps/integrations/tests/test_sidebar_ai_endpoints.py -v`
Expected: all briefing + chat tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/integrations/views.py apps/integrations/urls.py apps/integrations/tests/test_sidebar_ai_endpoints.py
git commit -m "feat(integrations): add claim-scoped Zendesk sidebar chat endpoint"
```

---

### Task 5: Backend regression check

- [ ] **Step 1: Run the integrations + ai + agent suites**

Run: `.venv/bin/pytest apps/integrations/ apps/ai/ apps/agent/ -q --tb=short`
Expected: all green (no new failures vs. before this phase).

- [ ] **Step 2: Confirm URLs resolve**

Run: `.venv/bin/python manage.py check`
Expected: `System check identified no issues`.

(No commit — verification only.)

---

## PHASE B — The Zendesk app (ZAF v2)

> These files are a self-contained ZAF app under `zendesk_app/`. They are NOT imported by Django. The frontend is verified manually via `zcli apps:server` (Task 10); there is no pytest for it.

### Task 6: `manifest.json`

**Files:** Create `zendesk_app/manifest.json`

- [ ] **Step 1: Create the manifest**

```json
{
  "name": "LORA Claim Assistant",
  "author": { "name": "Airport Lost Found", "email": "alexandru.radulescu@neurony.ro" },
  "defaultLocale": "en",
  "private": true,
  "location": { "support": { "ticket_sidebar": "assets/iframe.html" } },
  "version": "1.0.0",
  "frameworkVersion": "2.0",
  "domainWhitelist": ["alfapp-production.up.railway.app", "lora.airportlostfound.com"],
  "parameters": [
    { "name": "lora_base_url", "type": "text", "required": true,
      "default": "https://lora.airportlostfound.com" },
    { "name": "sidebar_secret_token", "type": "text", "required": true, "secure": true }
  ]
}
```

- [ ] **Step 2: Commit**

```bash
git add zendesk_app/manifest.json
git commit -m "feat(zendesk-app): add ZAF manifest (ticket sidebar + secure settings)"
```

### Task 7: `iframe.html` + `styles.css`

**Files:** Create `zendesk_app/assets/iframe.html`, `zendesk_app/assets/styles.css`

- [ ] **Step 1: Create `iframe.html`**

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <div id="tabs">
    <button id="tab-briefing" class="tab active" data-tab="briefing">Briefing</button>
    <button id="tab-chat" class="tab" data-tab="chat">Chat</button>
  </div>

  <section id="panel-briefing" class="panel">
    <div id="briefing-loading" class="muted">Loading briefing…</div>
    <div id="briefing-content" hidden>
      <div id="summary" class="summary"></div>
      <div id="next-steps"></div>
      <div id="facts" class="facts"></div>
    </div>
    <div id="briefing-error" class="error" hidden>
      Couldn't load the briefing. <button id="briefing-retry">Retry</button>
    </div>
  </section>

  <section id="panel-chat" class="panel" hidden>
    <div id="chat-log"></div>
    <form id="chat-form">
      <input id="chat-input" type="text" placeholder="Ask about this claim…" autocomplete="off">
      <button type="submit">→</button>
    </form>
  </section>

  <script src="https://static.zdassets.com/zendesk_app_framework_sdk/2.0/zaf_sdk.min.js"></script>
  <script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create `styles.css`**

```css
body { font: 13px/1.45 -apple-system, system-ui, sans-serif; color: #2f3941; margin: 0; padding: 8px; }
#tabs { display: flex; border-bottom: 1px solid #d8dcde; margin-bottom: 8px; }
.tab { flex: 1; background: none; border: none; padding: 8px; cursor: pointer; color: #68737d; font-weight: 600; }
.tab.active { color: #1f73b7; border-bottom: 2px solid #1f73b7; }
.summary { background: #f0f7ff; border-left: 3px solid #1f73b7; padding: 8px; border-radius: 4px; }
#next-steps ul { margin: 8px 0; padding-left: 18px; }
.facts { margin-top: 10px; line-height: 1.7; }
.facts .pill { display: inline-block; background: #fff0e1; color: #ad5e18; padding: 1px 8px; border-radius: 10px; margin-right: 4px; }
.muted { color: #68737d; } .error { color: #b81a1a; }
#chat-log { max-height: 320px; overflow-y: auto; margin-bottom: 8px; }
.msg { margin: 6px 0; padding: 6px 8px; border-radius: 6px; }
.msg.user { background: #e9eef2; } .msg.ai { background: #f0f7ff; }
#chat-form { display: flex; gap: 4px; }
#chat-input { flex: 1; padding: 6px; border: 1px solid #d8dcde; border-radius: 4px; }
#chat-form button { background: #1f73b7; color: #fff; border: none; border-radius: 4px; padding: 6px 10px; cursor: pointer; }
```

- [ ] **Step 3: Commit**

```bash
git add zendesk_app/assets/iframe.html zendesk_app/assets/styles.css
git commit -m "feat(zendesk-app): tabbed panel shell + styles"
```

### Task 8: `app.js` (ZAF logic)

**Files:** Create `zendesk_app/assets/app.js`

- [ ] **Step 1: Create `app.js`**

```javascript
const client = ZAFClient.init();
let history = [];

client.invoke('resize', { width: '100%', height: '520px' });

// --- helpers ---
async function loraRequest(path, body) {
  const settings = await client.metadata().then(m => m.settings);
  const opts = {
    url: settings.lora_base_url.replace(/\/$/, '') + path,
    type: 'POST',
    contentType: 'application/json',
    data: JSON.stringify(body),
  };
  if (settings.sidebar_secret_token) {
    // zcli local server: no secure-settings support, so the value typed at the
    // zcli prompt is exposed here — send it directly. Installed apps never
    // expose secure settings to the browser, so this branch is dev-only.
    opts.headers = { Authorization: 'Bearer ' + settings.sidebar_secret_token };
  } else {
    // Installed app: Zendesk's proxy substitutes the secure setting server-side.
    // Requires secure:true and the domain in manifest.json domainWhitelist.
    opts.headers = { Authorization: 'Bearer {{setting.sidebar_secret_token}}' };
    opts.secure = true;
  }
  return client.request(opts);
}

async function ticketContext() {
  const data = await client.get([
    'ticket.id', 'ticket.subject', 'ticket.description',
    'ticket.requester.email', 'ticket.comments',
  ]);
  return {
    ticket_id: String(data['ticket.id']),
    subject: data['ticket.subject'] || '',
    description: data['ticket.description'] || '',
    requester_email: data['ticket.requester.email'] || '',
    comments: (data['ticket.comments'] || []).map(c => c.value).slice(0, 10),
  };
}

// --- tabs ---
document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const which = tab.dataset.tab;
    document.getElementById('panel-briefing').hidden = which !== 'briefing';
    document.getElementById('panel-chat').hidden = which !== 'chat';
  };
});

// --- briefing ---
async function loadBriefing() {
  const loading = document.getElementById('briefing-loading');
  const content = document.getElementById('briefing-content');
  const errorEl = document.getElementById('briefing-error');
  loading.hidden = false; content.hidden = true; errorEl.hidden = true;
  try {
    const ctx = await ticketContext();
    const resp = await loraRequest('/api/integrations/zd/briefing/', ctx);
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    document.getElementById('summary').textContent = data.summary || '';
    const steps = (data.next_steps || []).map(s => `<li>${escapeHtml(s)}</li>`).join('');
    document.getElementById('next-steps').innerHTML = steps ? `<strong>Next steps:</strong><ul>${steps}</ul>` : '';
    const f = data.facts || {};
    document.getElementById('facts').innerHTML = renderFacts(f);
    loading.hidden = true; content.hidden = false;
  } catch (e) {
    loading.hidden = true; errorEl.hidden = false;
  }
}

function renderFacts(f) {
  if (!f || !Object.keys(f).length) return '<span class="muted">No linked LORA claim.</span>';
  const bits = [];
  if (f.status) bits.push(`<span class="pill">${escapeHtml(f.status)}</span>`);
  if (f.deadline) bits.push(`<span class="pill">Deadline ${escapeHtml(f.deadline)}</span>`);
  let html = `<div>${bits.join(' ')}</div>`;
  if (f.emails_total != null) html += `<div>✉️ ${f.emails_total} emails · <b>${f.emails_unresolved || 0} need action</b></div>`;
  if (f.disputes_total != null) html += `<div>💳 ${f.disputes_total} disputes</div>`;
  return html;
}

document.getElementById('briefing-retry').onclick = loadBriefing;

// --- chat ---
const chatLog = document.getElementById('chat-log');
document.getElementById('chat-form').onsubmit = async (ev) => {
  ev.preventDefault();
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  appendMsg('user', msg);
  input.value = '';
  history.push({ role: 'user', content: msg });
  try {
    const ctx = await ticketContext();
    const resp = await loraRequest('/api/integrations/zd/chat/', {
      ticket_id: ctx.ticket_id, message: msg, history: history,
    });
    const data = typeof resp === 'string' ? JSON.parse(resp) : resp;
    appendMsg('ai', data.answer || '(no answer)');
    history.push({ role: 'assistant', content: data.answer || '' });
  } catch (e) {
    appendMsg('ai', 'Sorry — something went wrong reaching LORA.');
  }
};

function appendMsg(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// init
loadBriefing();
```

- [ ] **Step 2: Commit**

```bash
git add zendesk_app/assets/app.js
git commit -m "feat(zendesk-app): ZAF logic — load briefing + claim-scoped chat via proxied requests"
```

### Task 9: translations + logo

**Files:** Create `zendesk_app/translations/en.json`, `zendesk_app/assets/logo.png`

- [ ] **Step 1: Create `translations/en.json`**

```json
{
  "app": {
    "name": "LORA Claim Assistant",
    "short_description": "AI briefing + chat for lost-item claims, inside the ticket.",
    "long_description": "Shows an AI summary, key facts, and a claim-scoped chat assistant powered by LORA.",
    "installation_instructions": "Set your LORA base URL and the sidebar secret token in the app settings."
  }
}
```

- [ ] **Step 2: Add a logo**

Add any 128x128 PNG at `zendesk_app/assets/logo.png` (placeholder is fine for a private app). If you don't have one: `python -c "from PIL import Image; Image.new('RGB',(128,128),'#03363d').save('zendesk_app/assets/logo.png')"` from the project root (Pillow is already installed).

- [ ] **Step 3: Commit**

```bash
git add zendesk_app/translations/en.json zendesk_app/assets/logo.png
git commit -m "feat(zendesk-app): translations + logo"
```

---

## PHASE C — Dev workflow & docs

### Task 10: App README + deployment notes

**Files:** Create `zendesk_app/README.md`; modify `docs/DEPLOYMENT.md`

- [ ] **Step 1: Create `zendesk_app/README.md`**

````markdown
# LORA Claim Assistant — Zendesk App

A ticket-sidebar app: AI briefing + claim-scoped chat, backed by LORA.

## Prerequisites
- Zendesk plan that allows **private apps** (Support **Team** plan and up).
- Node + Zendesk CLI: `npm install -g @zendesk/zcli`
- LORA running with `sidebar_secret_token` set in SystemSettings.

## Local development (live, no upload)
```bash
cd zendesk_app
zcli apps:server
# then in Zendesk: append ?zcli_apps=true to a ticket URL to load the local app
```
Set the two settings when prompted: `lora_base_url` (your LORA URL) and `sidebar_secret_token`.

## First install (upload as a private app)
```bash
cd zendesk_app
zcli login -i        # authenticate to your Zendesk subdomain
zcli apps:create     # packages + uploads, creates the private app
```
Then set the app settings (LORA URL + secret token) in Admin → Apps.

## Updating after changes
```bash
cd zendesk_app
zcli apps:update     # re-packages and pushes the new version to the installed app
```
Note: updates are **immediate for all agents** — there is no staging. Run this deliberately.
````

- [ ] **Step 2: Add a short section to `docs/DEPLOYMENT.md`**

Append a section "## 11. Zendesk sidebar app" summarizing: the app lives in `zendesk_app/`, deploys separately from the Django backend via `zcli`, manual `zcli apps:update` is the default, and an optional GitHub Actions workflow (running `zcli apps:update` with `ZENDESK_*` secrets on push) can automate it later — with the caveat that updates hit all agents instantly, so gate it behind a release tag if you enable it.

- [ ] **Step 3: Commit**

```bash
git add zendesk_app/README.md docs/DEPLOYMENT.md
git commit -m "docs(zendesk-app): dev/install/update workflow + deployment notes"
```

---

## Post-completion checklist

- [ ] `.venv/bin/pytest apps/integrations/ apps/ai/ apps/agent/ -q` — all green
- [ ] `manage.py check` clean; both new routes resolve (`zd/briefing/`, `zd/chat/`)
- [ ] `zcli apps:server` shows the panel in a real ticket; briefing loads; chat answers and stays scoped to the ticket's claim
- [ ] Confirm Zendesk plan supports private apps before `zcli apps:create`
- [ ] `sidebar_secret_token` set in SystemSettings AND as the app's secure setting
- [ ] Verify PII: in LORA logs, confirm the briefing/chat calls show tokenized placeholders, not real client emails

## Notes / deferred
- **Action buttons** (browser-use form-fill, dispute docs, PayPal submit) are intentionally not built here. They extend the same pattern: a new LORA endpoint + a button in `app.js`.
- If you later want per-agent identity/audit instead of one shared token, that's a separate auth upgrade (OAuth/JWT between Zendesk and LORA).

---

## AS-BUILT ADDENDUM (2026-06-11) — what shipped beyond this plan

The plan above was executed in full, then extended during live testing. The
app is **installed in production** (app_id 1260824 on airportlf.zendesk.com).
Current truth lives in `zendesk_app/README.md` + the code; summary of
post-plan increments:

1. **ZAF SDK URL fix** — the plan's SDK path was wrong (no such file);
   correct: `static.zdassets.com/zendesk_app_framework_sdk/2.0/zaf_sdk.min.js`.
2. **Secure-settings auth fix** — `{{setting.*}}` substitution requires
   `secure: true` + `domainWhitelist` in the manifest; the zcli local server
   doesn't support secure settings at all, so `app.js` falls back to the
   locally-exposed value during dev.
3. **Diagnosable errors** — panel failures show HTTP status + actionable hint.
4. **Ticket-content chat** — `/zd/chat/` answers from ticket content when no
   claim is linked (was: refusal).
5. **Prompt/input overhaul** — comments sent as
   `[{author, created_at, public, text}]` (30×1500, chronological, via Zendesk
   REST from the app) + `ticket_created_at` + `requester_name`; shared
   `ALF_BUSINESS_CONTEXT` business preamble in `apps/integrations/views.py`;
   briefing leads with lifecycle stage; `mode='next_steps'` generates steps on
   demand (NextSteps schema).
6. **NAME tokenization** — `RegexTokenizer(known_names=...)`: full client name
   any casing, Capitalized/ALL-CAPS parts ≥3 chars; views pass
   `known_pii={'names': [requester_name, claim.client_name]}`. Bracket-less
   placeholder echoes (NAME_xxxx without <>) are restored too.
7. **Agent features** — `POST /zd/draft/` (client_update | institution_reply →
   EmailDraft → inserted via `ticket.editor.insert`, never auto-sent);
   "Needs attention" unresolved-emails block (response-only, kept OUT of the
   trusted AI channel); `facts.next_update_due` (day 2/5/11/20 cadence);
   chat translation capability.
8. **Visual pass** — real logos (logo.png + logo-small.png), 2×2 SVG-icon
   action grid, skeleton loading, chat layout with bottom composer + suggestion
   chips, deadline urgency pills.

Test coverage at install time: **280 passing** across integrations/ai/agent.
