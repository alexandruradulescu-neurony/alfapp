# Claim-detail screen rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the single-claim screen as a decluttered, manager-facing snapshot — two columns (work left, reference facts right, collapsed by default), one status, and in-place updates via HTMX + Alpine — with no change to what any action does.

**Architecture:** The whole screen body becomes one re-renderable fragment (`agent/_claim_body.html`) fed by a shared context helper. The seven same-app form actions return that fragment on HTMX requests (instead of redirecting); the five cross-app JSON API endpoints keep their contracts and the page refreshes the fragment after them via one small helper. Alpine handles menu/expand/modal toggles. Visual styling stays flat and gets a modest, centralized token refresh.

**Tech Stack:** Django 5.2 server-rendered templates, Tailwind v4 + DaisyUI v5 (`lora` theme, compiled CSS committed), HTMX + Alpine (vendored into `static/js/vendor/`), Bootstrap Icons, pytest + Django `TestCase`.

---

## Pre-flight (read once before starting)

- **Branch:** work on `feat/frontend-redesign-claim-detail` (already created; the spec lives there).
- **Spec:** `docs/superpowers/specs/2026-06-18-frontend-redesign-claim-detail-pilot-design.md`.
- **Run tests:** `.venv/bin/python -m pytest <path> -o addopts=""` (the repo's `addopts` add `-v/--strict-markers`; override with `-o addopts=""` per project convention). `python` is not on PATH — always use `.venv/bin/python`. SQLite tests — don't crank parallelism.
- **Build CSS:** `npm run build` (compiles `static/src/css/tailwind.css` → `static/css/tailwind.css`). The deploy does **not** rebuild CSS — the compiled file is git-tracked and must be committed.
- **No business-logic changes.** Every endpoint keeps its current behavior and contract. The template restructure is behavior-preserving presentation; the only genuinely new behavior is the `HX-Request` branch on the 7 form views, one new GET fragment route, and the nav-routing fix — those get real red-green tests. (Per project TDD convention, behavior-preserving work leans on the existing suite as the net.)
- **Role kwarg is dead.** The manager/agent role split was removed — create test users with `User.objects.create_user(username=..., password='x')` (no `role=`). The `@agent_required`/`@manager_required` decorators stay on views untouched (they no longer gate by role).
- **Key fact:** HTMX is green-field here — no existing `HX-Request`/`htmx` usage anywhere.

### The "claim body" fragment model (the core idea)

Today `agent_claim_detail` renders `agent/claim_detail.html` (extends `base.html`) from this context (verbatim keys): `claim`, `zd_subdomain`, `claim_refund_status`, `emails_open`, `emails_handled`, `client_followups`.

We split that into:
- `agent/claim_detail.html` — extends `base.html`, just `{% include 'agent/_claim_body.html' %}`.
- `agent/_claim_body.html` — the entire two-column screen, root element `<div id="claim-body" hx-target="this" hx-swap="outerHTML"> … </div>`, including a toast region that renders Django `messages`.

Every mutating action returns a fresh `_claim_body.html`, and HTMX swaps `#claim-body` in place — no full reload, no scroll jump. `hx-target="this"`/`hx-swap="outerHTML"` on the root means buttons inside don't each need targets.

---

## File Structure

**Create:**
- `static/js/vendor/htmx.min.js` — vendored HTMX (pinned).
- `static/js/vendor/alpine.min.js` — vendored Alpine (pinned).
- `static/js/lora-htmx.js` — ~30-line helper: global CSRF header for HTMX, `refreshClaimBody()`, and `toast()` for the JSON-endpoint buttons. Replaces the old per-screen inline JS.
- `templates/agent/_claim_body.html` — the two-column screen body (root `#claim-body`).
- `templates/agent/partials/_claim_header.html` — compact header (name, object, one status, urgent chips, action bar, `···` menu).
- `templates/agent/partials/_client_communication.html` — main update + follow-ups list.
- `templates/agent/partials/_institution_replies.html` — per-office incoming emails (open floated up, handled collapsed).
- `templates/agent/partials/_reference_cards.html` — Status / Client / Case / Flight / Refunds-&-evidence, collapsed by default.
- `templates/partials/_toast.html` — shared toast/messages region (renders `messages`).
- `apps/users/tests/test_claim_detail_htmx.py` — tests for the fragment route + the 7 form views' HX-Request branch.
- `apps/users/tests/test_claim_nav.py` — test for the nav-routing fix.

**Modify:**
- `apps/users/views.py` — extract `_claim_detail_context`; add `agent_claim_detail_body`; add `_claim_detail_response`; rewire the 7 form views' return.
- `apps/users/urls.py` — add the body fragment route.
- `templates/base.html` — load HTMX + Alpine + `lora-htmx.js`; fix the sidebar "Claims" link; include shared toast region.
- `templates/agent/claim_detail.html` — shrink to extend + include the body partial.
- `static/src/css/tailwind.css` — modest token refresh; then `npm run build`.
- `static/css/tailwind.css` — committed compiled output (regenerated, not hand-edited).
- `apps/users/tests/test_page_smoke.py` — extend with endpoint-preservation assertions for the rebuilt screen.

---

## Phase 1 — Foundation: vendor HTMX + Alpine, wire base.html

### Task 1: Vendor HTMX and Alpine into static

**Files:**
- Create: `static/js/vendor/htmx.min.js`
- Create: `static/js/vendor/alpine.min.js`

- [ ] **Step 1: Download pinned versions into the vendor folder**

```bash
mkdir -p static/js/vendor
curl -sL https://cdnjs.cloudflare.com/ajax/libs/htmx/2.0.4/htmx.min.js -o static/js/vendor/htmx.min.js
curl -sL https://cdnjs.cloudflare.com/ajax/libs/alpinejs/3.14.8/cdn.min.js -o static/js/vendor/alpine.min.js
```

- [ ] **Step 2: Verify the files downloaded and are non-empty JS (not an error page)**

Run: `head -c 200 static/js/vendor/htmx.min.js; echo; wc -c static/js/vendor/*.js`
Expected: HTMX file starts with its minified banner/code, both files are tens of KB (not a few hundred bytes of HTML error).

- [ ] **Step 3: Commit**

```bash
git add static/js/vendor/htmx.min.js static/js/vendor/alpine.min.js
git commit -m "chore(frontend): vendor htmx + alpine (no runtime CDN, no npm install)"
```

### Task 2: Create the HTMX helper (CSRF + refresh + toast)

**Files:**
- Create: `static/js/lora-htmx.js`

- [ ] **Step 1: Write the helper**

```javascript
(function () {
  function csrfToken() {
    var el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute('content') : '';
  }

  document.body.addEventListener('htmx:configRequest', function (evt) {
    evt.detail.headers['X-CSRFToken'] = csrfToken();
  });

  window.toast = function (message, kind) {
    var region = document.getElementById('toast-region');
    if (!region || !message) return;
    var el = document.createElement('div');
    el.setAttribute('role', 'alert');
    el.className = 'lora-toast lora-toast-' + (kind || 'info');
    el.textContent = message;
    region.appendChild(el);
    setTimeout(function () { el.remove(); }, 3500);
  };

  window.refreshClaimBody = function (bodyUrl) {
    window.htmx.ajax('GET', bodyUrl, { target: '#claim-body', swap: 'outerHTML' });
  };
})();
```

- [ ] **Step 2: Commit**

```bash
git add static/js/lora-htmx.js
git commit -m "feat(frontend): htmx CSRF + claim-body refresh + toast helper"
```

### Task 3: Wire HTMX, Alpine, helper, and the shared toast region into base.html

**Files:**
- Modify: `templates/base.html` (head + before `</body>`)
- Create: `templates/partials/_toast.html`

- [ ] **Step 1: Create the shared toast/messages region**

`templates/partials/_toast.html`:
```html
<div id="toast-region" class="lora-toast-region" aria-live="polite" aria-atomic="false">
  {% if messages %}
    {% for message in messages %}
      <div role="alert" class="lora-toast lora-toast-{{ message.tags }}">{{ message }}</div>
    {% endfor %}
  {% endif %}
</div>
```

- [ ] **Step 2: Load the scripts in base.html**

In `templates/base.html`, replace the `{% block extra_js %}{% endblock %}` line (near `</body>`) with:
```html
    <script src="{% static 'js/vendor/htmx.min.js' %}" defer></script>
    <script src="{% static 'js/vendor/alpine.min.js' %}" defer></script>
    <script src="{% static 'js/lora-htmx.js' %}" defer></script>
    {% block extra_js %}{% endblock %}
```
(Alpine must load after HTMX; `lora-htmx.js` after HTMX. `defer` preserves order.)

- [ ] **Step 3: Place the shared toast region in base.html**

In `templates/base.html`, immediately inside `<main>` (just before `{% block content %}`), the existing inline `{% if messages %}…{% endif %}` alert block (currently lines ~69-79) is **replaced** by:
```html
                {% include 'partials/_toast.html' %}
```
(Removes the old DaisyUI alert loop; the toast region now owns message display app-wide.)

- [ ] **Step 4: Add the toast styles (source CSS)**

Append to `static/src/css/tailwind.css`:
```css
/* ── Toasts (HTMX-friendly, replaces old inline showToast) ── */
.lora-toast-region { position: fixed; top: 1rem; right: 1rem; z-index: 60; display: flex; flex-direction: column; gap: .5rem; }
.lora-toast { background: #fff; border: 1px solid oklch(0.92 0.01 260); border-radius: .75rem; padding: .625rem .875rem; font-size: .875rem; box-shadow: 0 4px 12px oklch(0.5 0 0 / 0.08); }
.lora-toast-success { border-left: 3px solid #10b981; }
.lora-toast-error { border-left: 3px solid #ef4444; }
.lora-toast-warning { border-left: 3px solid #f59e0b; }
.lora-toast-info { border-left: 3px solid #3b82f6; }
```

- [ ] **Step 5: Rebuild CSS and run the smoke test (nothing should break yet)**

Run: `npm run build && .venv/bin/python -m pytest apps/users/tests/test_page_smoke.py -o addopts=""`
Expected: build succeeds; all smoke tests PASS (pages still render 200 with scripts added).

- [ ] **Step 6: Commit**

```bash
git add templates/base.html templates/partials/_toast.html static/src/css/tailwind.css static/css/tailwind.css
git commit -m "feat(frontend): load htmx+alpine app-wide; shared toast region"
```

---

## Phase 2 — Refactor the view into a shared context + body fragment

### Task 4: Extract `_claim_detail_context` and add the body fragment route

**Files:**
- Modify: `apps/users/views.py` (the `agent_claim_detail` function, currently ~lines 410-449)
- Modify: `apps/users/urls.py`
- Test: `apps/users/tests/test_claim_detail_htmx.py`

- [ ] **Step 1: Write the failing test for the new body route**

`apps/users/tests/test_claim_detail_htmx.py`:
```python
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.claims.models import Claim

User = get_user_model()


class ClaimBodyFragmentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='body_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='b@example.com', client_name='Bo Li',
            zd_ticket_id='95001', alf_claim_id='ALF95001',
            price_paid=Decimal('50.00'))

    def test_body_route_returns_fragment_not_full_page(self):
        resp = self.web.get(reverse('agent_claim_detail_body', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('id="claim-body"', html)
        self.assertNotIn('<html', html)  # fragment, not the full base shell

    def test_full_page_still_renders_and_contains_body(self):
        resp = self.web.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('<html', html)
        self.assertIn('id="claim-body"', html)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_detail_htmx.py -o addopts=""`
Expected: FAIL — `agent_claim_detail_body` route does not exist (NoReverseMatch), and `id="claim-body"` not present.

- [ ] **Step 3: Refactor the view to a shared context helper + add the body view**

In `apps/users/views.py`, replace the body of `agent_claim_detail` with a helper-based version and add the fragment view directly beneath it:
```python
def _claim_detail_context(claim_id):
    """Build the context for the single-claim screen (full page and HTMX body)."""
    claim = get_object_or_404(
        Claim.objects.prefetch_related(
            'evidence', 'emails', 'refunds', 'disputes', 'follow_up_updates'
        ).select_related('assigned_to'),
        id=claim_id,
    )
    _annotate_deadline(claim, timezone.now())

    try:
        system_settings = SystemSettings.get_instance()
        zd_subdomain = system_settings.zd_subdomain
    except Exception:
        zd_subdomain = ''

    all_emails = list(claim.emails.all())
    emails_open = [e for e in all_emails if e.action_required]
    emails_handled = [e for e in all_emails if not e.action_required]

    now = timezone.now()
    client_followups = list(claim.follow_up_updates.all())
    for fu in client_followups:
        fu.is_due = (fu.state == 'SCHEDULED' and fu.due_at <= now)

    return claim, {
        'claim': claim,
        'zd_subdomain': zd_subdomain,
        'claim_refund_status': claim.refund_status,
        'emails_open': emails_open,
        'emails_handled': emails_handled,
        'client_followups': client_followups,
    }


@agent_required
def agent_claim_detail(request, claim_id):
    """Agent claim detail view (full page)."""
    _claim, context = _claim_detail_context(claim_id)
    return render(request, 'agent/claim_detail.html', context)


@agent_required
def agent_claim_detail_body(request, claim_id):
    """The claim-detail screen body as an HTMX fragment (no base shell)."""
    _claim, context = _claim_detail_context(claim_id)
    return render(request, 'agent/_claim_body.html', context)
```

- [ ] **Step 4: Add the route**

In `apps/users/urls.py`, directly below the existing `agent/claims/<int:claim_id>/` line, add:
```python
    path('agent/claims/<int:claim_id>/body/', views.agent_claim_detail_body, name='agent_claim_detail_body'),
```

- [ ] **Step 5: Create a minimal `_claim_body.html` and shrink `claim_detail.html` so the route renders**

For now, move the **entire current contents** of `templates/agent/claim_detail.html` that sit inside `{% block content %}…{% endblock %}` into `templates/agent/_claim_body.html`, wrapped in the body root element. That is:

`templates/agent/_claim_body.html` (structure — existing markup goes inside):
```html
<div id="claim-body" hx-target="this" hx-swap="outerHTML">
  {% include 'partials/_toast.html' %}
  {# existing claim_detail content (sections, sidebars, modal) goes here verbatim for now; #}
  {# it gets reorganized into the two-column layout in Phase 4. #}
</div>
```

`templates/agent/claim_detail.html` becomes:
```html
{% extends 'base.html' %}
{% block title %}Claim {{ claim.alf_claim_id|default:claim.id }}{% endblock %}
{% block content %}{% include 'agent/_claim_body.html' %}{% endblock %}
```
Move the page's inline `<script>` (currently in `claim_detail.html`) along with the body content into `_claim_body.html` for now — it will be removed in Phase 4. Keep `{% load static %}`/other `{% load %}` tags at the top of `_claim_body.html` if the moved markup uses them.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_detail_htmx.py apps/users/tests/test_claim_detail_page.py apps/users/tests/test_page_smoke.py -o addopts=""`
Expected: PASS (fragment route returns body; full page still renders and includes `#claim-body`; existing page tests still green).

- [ ] **Step 7: Commit**

```bash
git add apps/users/views.py apps/users/urls.py templates/agent/claim_detail.html templates/agent/_claim_body.html apps/users/tests/test_claim_detail_htmx.py
git commit -m "refactor(claims): claim-detail body as HTMX fragment + shared context helper"
```

---

## Phase 3 — Make the seven form actions return the fragment on HTMX

### Task 5: Add `_claim_detail_response` and rewire the 7 form views

**Files:**
- Modify: `apps/users/views.py` (views: `client_updates_start`, `claim_acknowledge_risk`, `claim_client_report_send`, `claim_client_report_generate`, `client_followup_send`, `client_followup_prepare`, `client_followup_skip`)
- Test: `apps/users/tests/test_claim_detail_htmx.py` (extend)

- [ ] **Step 1: Write the failing tests for the HX-Request branch**

Append to `apps/users/tests/test_claim_detail_htmx.py`:
```python
class FormActionHtmxTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='act_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='a@example.com', client_name='Ada Min',
            zd_ticket_id='95002', alf_claim_id='ALF95002',
            price_paid=Decimal('50.00'))

    def test_acknowledge_risk_htmx_returns_body_fragment(self):
        resp = self.web.post(
            reverse('claim_acknowledge_risk', args=[self.claim.id]),
            HTTP_HX_REQUEST='true')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('id="claim-body"', html)
        self.assertNotIn('<html', html)

    def test_acknowledge_risk_non_htmx_still_redirects(self):
        resp = self.web.post(reverse('claim_acknowledge_risk', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/agent/claims/{self.claim.id}/', resp['Location'])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_detail_htmx.py::FormActionHtmxTests -o addopts=""`
Expected: FAIL — the HX-Request POST currently returns 302, not a 200 fragment.

- [ ] **Step 3: Add the response helper**

In `apps/users/views.py`, directly below `agent_claim_detail_body`, add:
```python
def _claim_detail_response(request, claim_id):
    """After a form action: HTMX gets the refreshed body fragment; plain
    requests keep the existing full-page redirect (no-JS fallback)."""
    if request.headers.get('HX-Request'):
        _claim, context = _claim_detail_context(claim_id)
        return render(request, 'agent/_claim_body.html', context)
    return redirect('agent_claim_detail', claim_id=claim_id)
```

- [ ] **Step 4: Rewire each of the 7 views' final return**

In each of the 7 views, replace the **final** `return redirect('agent_claim_detail', claim_id=…)` with `return _claim_detail_response(request, claim_id)`. The five views keyed by `update_id` resolve the claim via `_followup_and_claim`; for those use `return _claim_detail_response(request, claim.id)`. Leave the **guard-clause** early returns (method-not-POST, already-sent, empty-body, no-ticket, risk-active) as plain `redirect(...)` — those non-HTMX guards are fine as redirects, and on HTMX the relevant button won't be shown for those states anyway. (Behavior preserved: success path + messages identical; only the transport changes for HTMX callers.)

Concretely, the tail of e.g. `claim_acknowledge_risk`:
```python
    if claim.risk_active:
        claim.acknowledge_risk(request.user)
        messages.success(request, 'Risk flag acknowledged.')
    return _claim_detail_response(request, claim_id)
```
And e.g. `client_followup_skip`:
```python
    if request.method == 'POST':
        from apps.communications import client_updates as cu
        cu.skip_follow_up(update)
        messages.success(request, f'{update.label} update skipped.')
    return _claim_detail_response(request, claim.id)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_detail_htmx.py -o addopts=""`
Expected: PASS — HX-Request returns the 200 body fragment; non-HTMX still redirects.

- [ ] **Step 6: Run the broader claims/communications suite for regressions**

Run: `.venv/bin/python -m pytest apps/users apps/communications -o addopts=""`
Expected: PASS (no behavior regressions in the existing form-action tests).

- [ ] **Step 7: Commit**

```bash
git add apps/users/views.py apps/users/tests/test_claim_detail_htmx.py
git commit -m "feat(claims): 7 claim-detail form actions return body fragment on HTMX"
```

---

## Phase 4 — Rebuild the screen into the two-column layout

> These tasks reorganize existing markup into the approved layout and wire HTMX/Alpine. The data each section receives is unchanged (same context keys). Verification is preservation-based: the screen renders 200, every action endpoint URL is still present, and the inline `<script>` block is gone (replaced by `lora-htmx.js` + Alpine attributes). Build markup from the section inventory in the spec and the existing `agent/claim_detail.html`; do not invent new endpoints or fields.

### Task 6: Endpoint-preservation guard test (write first, keep green through Phase 4)

**Files:**
- Modify: `apps/users/tests/test_page_smoke.py`

- [ ] **Step 1: Add a test asserting the rebuilt screen still exposes every action**

Append to `apps/users/tests/test_page_smoke.py`:
```python
from decimal import Decimal as _D


class ClaimDetailControlsPreservedTests(TestCase):
    """The redesign is presentation-only — every action the screen drove must
    still be reachable from the rendered HTML."""

    def setUp(self):
        self.user = User.objects.create_user(username='ctrl_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)
        self.claim = Claim.objects.create(
            client_email='c@example.com', client_name='Cy Ng',
            zd_ticket_id='96001', alf_claim_id='ALF96001',
            price_paid=_D('60.00'))

    def test_action_endpoints_present_in_rendered_screen(self):
        from django.urls import reverse
        resp = self.web.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # Same-app form action URLs
        for name in ['claim_client_report_send', 'claim_client_report_generate',
                     'client_updates_start']:
            self.assertIn(reverse(name, args=[self.claim.id]), html,
                          f"{name} URL missing from rebuilt screen")
        # Cross-app JSON endpoints (string fragments, since they're DRF routes)
        self.assertIn(f'/api/claims/{self.claim.id}/update-from-zendesk/', html)
        self.assertIn(f'/api/claims/{self.claim.id}/check-email/', html)

    def test_no_inline_script_block_remains(self):
        from django.urls import reverse
        resp = self.web.get(reverse('agent_claim_detail', args=[self.claim.id]))
        html = resp.content.decode()
        # The 240-line inline <script> is replaced by lora-htmx.js + Alpine attrs.
        self.assertNotIn('function updateFromZendesk', html)
        self.assertNotIn('function checkEmail', html)
```

- [ ] **Step 2: Run — expect the controls test to PASS now (markup still present from Phase 2 move) and `test_no_inline_script_block_remains` to FAIL**

Run: `.venv/bin/python -m pytest apps/users/tests/test_page_smoke.py::ClaimDetailControlsPreservedTests -o addopts=""`
Expected: `test_action_endpoints_present_in_rendered_screen` PASS; `test_no_inline_script_block_remains` FAIL (inline JS still present from the Phase 2 move). This is the red we drive to green across Tasks 7-10.

### Task 7: Split the body into the four partials (header + two work panels + reference cards)

**Files:**
- Create: `templates/agent/partials/_claim_header.html`, `_client_communication.html`, `_institution_replies.html`, `_reference_cards.html`
- Modify: `templates/agent/_claim_body.html`

- [ ] **Step 1: Reassemble `_claim_body.html` to the two-column structure**

```html
{% load static %}
<div id="claim-body" hx-target="this" hx-swap="outerHTML">
  {% include 'partials/_toast.html' %}
  {% include 'agent/partials/_claim_header.html' %}
  <div class="grid grid-cols-1 lg:grid-cols-[1.55fr_1fr] gap-4 mt-4">
    <div class="flex flex-col gap-4">
      {% include 'agent/partials/_client_communication.html' %}
      {% include 'agent/partials/_institution_replies.html' %}
    </div>
    <div class="flex flex-col gap-2">
      {% include 'agent/partials/_reference_cards.html' %}
    </div>
  </div>
</div>
```

- [ ] **Step 2: Populate each partial from the existing markup**

Move the corresponding sections out of the old content into each partial, preserving every control, `{% url %}`, form field name, and DRF endpoint string:
- `_claim_header.html` — claim id/client/object; **one** primary status pill (`claim.status`); urgent chips only (risk flag if `claim.risk_active`, "N need action" from `emails_open|length`); action bar with `Send update` + `Check email`; a `···` menu (Alpine, Task 9) holding refresh-from-Zendesk, mark-as-disputed, delete, open-in-Zendesk.
- `_client_communication.html` — the main "what we did" update (`claim.client_report_*`, `client_updates_start`, `claim_client_report_send/generate`) and the `client_followups` list (`client_followup_send/prepare/skip`), surfacing due/sent first.
- `_institution_replies.html` — `emails_open` (floated up, danger styling) and `emails_handled` (collapsed) from the existing email-log markup; keep the resolve endpoint `/api/communications/email-logs/<id>/resolve/`.
- `_reference_cards.html` — Status / Client / Case / Flight / Refunds-&-evidence as collapse-by-default cards (Alpine, Task 9). Demote the Case/Refund/Dispute sub-badges and technical IDs (Zendesk #, PayPal id) into the Status/Refunds cards. The Grant-refund modal markup also lives here.

- [ ] **Step 3: Run smoke + controls tests**

Run: `.venv/bin/python -m pytest apps/users/tests/test_page_smoke.py apps/users/tests/test_claim_detail_page.py apps/users/tests/test_claim_detail_htmx.py -o addopts=""`
Expected: page renders 200; `test_action_endpoints_present_in_rendered_screen` PASS; fragment tests PASS. (`test_no_inline_script_block_remains` still FAIL until Task 8/10.)

- [ ] **Step 4: Commit**

```bash
git add templates/agent/_claim_body.html templates/agent/partials/
git commit -m "refactor(claims): two-column claim-detail layout split into partials"
```

### Task 8: Wire the 7 form actions and the body refresh with HTMX attributes

**Files:**
- Modify: the partials from Task 7

- [ ] **Step 1: Convert the 7 form actions to HTMX posts**

Each `<form method="post" action="{% url '…' %}">` in the communication partials gets `hx-post="{% url '…' %}"` (HTMX serializes and posts the form; `#claim-body` root `hx-target`/`hx-swap` handles the swap). Keep `{% csrf_token %}` inside the form and keep `method="post" action="…"` so the no-JS fallback still works. Example:
```html
<form method="post" action="{% url 'client_followup_skip' fu.id %}"
      hx-post="{% url 'client_followup_skip' fu.id %}">
  {% csrf_token %}
  <button type="submit" class="btn btn-sm btn-ghost rounded-xl">Skip</button>
</form>
```

- [ ] **Step 2: Wire the 5 JSON-endpoint buttons to refresh + toast**

For `Check email`, `Refresh from Zendesk`, email `Resolve`/`Reopen`, `Grant refund` submit, and `Delete`: post via HTMX to the existing JSON endpoint with `hx-swap="none"`, then refresh the body and toast on success. Use the helper from Task 2. Example (check-email):
```html
<button class="btn btn-sm rounded-xl"
        hx-post="/api/claims/{{ claim.id }}/check-email/"
        hx-swap="none"
        hx-on::after-request="if(event.detail.successful){window.refreshClaimBody('{% url 'agent_claim_detail_body' claim.id %}'); window.toast('Mailbox checked','success')}else{window.toast('Could not check mail','error')}">
  <i class="bi bi-envelope-arrow-down"></i> Check email
</button>
```
For `Delete`, on success redirect to the list instead of refreshing: `window.location.href='{% url 'agent_claims' %}'`. Keep the existing JS `confirm()` by using `hx-confirm="Delete this claim? This cannot be undone."`.

- [ ] **Step 3: Run smoke + controls tests**

Run: `.venv/bin/python -m pytest apps/users/tests/test_page_smoke.py apps/users/tests/test_claim_detail_htmx.py -o addopts=""`
Expected: still 200; endpoint-preservation PASS (URLs unchanged); fragment posts still work.

- [ ] **Step 4: Commit**

```bash
git add templates/agent/partials/
git commit -m "feat(claims): HTMX-wire claim-detail actions (in-place updates, no reload)"
```

### Task 9: Replace inline JS toggles with Alpine (menu, collapse, modal)

**Files:**
- Modify: `templates/agent/partials/_claim_header.html`, `_reference_cards.html`

- [ ] **Step 1: `···` menu via Alpine**

```html
<div x-data="{ open: false }" class="relative">
  <button @click="open = !open" class="btn btn-sm btn-ghost rounded-xl" aria-label="More actions"><i class="bi bi-three-dots"></i></button>
  <div x-show="open" @click.outside="open = false" x-cloak class="absolute right-0 mt-1 w-56 bg-base-100 border border-base-300 rounded-xl p-1 z-50">
    {# refresh-from-Zendesk, mark-as-disputed, delete, open-in-Zendesk items here #}
  </div>
</div>
```

- [ ] **Step 2: Reference cards collapse-by-default via Alpine**

Each reference card:
```html
<div x-data="{ open: false }" class="card-modern p-3">
  <button @click="open = !open" class="flex items-center gap-2 w-full text-left">
    <span class="text-sm font-medium">Case facts</span>
    <span class="ml-auto text-xs text-base-content/50">{{ claim.created_at|date:'M j' }}</span>
    <i class="bi" :class="open ? 'bi-chevron-down' : 'bi-chevron-right'"></i>
  </button>
  <div x-show="open" x-cloak class="mt-2">{# full case fields here #}</div>
</div>
```
(Status card opens by default only if you choose; spec says collapse all — keep `open: false` everywhere.)

- [ ] **Step 3: Refund modal via Alpine (replace the JS open/close + form handler)**

Wrap the modal trigger + dialog in one `x-data="{ open: false }"`; the trigger sets `open = true`; the submit posts via HTMX (Task 8 pattern) and closes on success in the `hx-on::after-request`. Add `[x-cloak]{display:none}` to the source CSS so `x-cloak` hides pre-init.

- [ ] **Step 4: Add the `x-cloak` style**

Append to `static/src/css/tailwind.css`:
```css
[x-cloak] { display: none !important; }
```

- [ ] **Step 5: Rebuild CSS, run tests**

Run: `npm run build && .venv/bin/python -m pytest apps/users/tests/test_page_smoke.py -o addopts=""`
Expected: build succeeds; pages render 200.

- [ ] **Step 6: Commit**

```bash
git add templates/agent/partials/ static/src/css/tailwind.css static/css/tailwind.css
git commit -m "feat(claims): Alpine for menu, collapsible reference cards, refund modal"
```

### Task 10: Remove the dead inline `<script>` and verify it's gone

**Files:**
- Modify: `templates/agent/_claim_body.html` (and partials, if any script moved there)

- [ ] **Step 1: Delete the old inline `<script>` block**

Remove the ~240-line `<script>…</script>` carried over in Phase 2 (functions `updateFromZendesk`, `checkEmail`, `deleteClaim`, `resolveEmail`, refund modal/handler, `getCsrfToken`, `showToast`). Their behavior now lives in `lora-htmx.js` + Alpine + HTMX attributes.

- [ ] **Step 2: Run the full screen test set**

Run: `.venv/bin/python -m pytest apps/users/tests/test_page_smoke.py apps/users/tests/test_claim_detail_page.py apps/users/tests/test_claim_detail_htmx.py -o addopts=""`
Expected: ALL PASS, including `test_no_inline_script_block_remains` (now green).

- [ ] **Step 3: Manual verification (real browser)**

Start the app, open a claim, and confirm in-place behavior: sending/regenerating/skipping an update, resolving/reopening an email, check-email, refresh-from-Zendesk, and a refund all update the screen *without* a full reload or scroll jump, with a toast. Disable JS and confirm the form actions still work via redirect (no-JS fallback). Use the project's run workflow (see the `run` skill / README) — do not install anything.

- [ ] **Step 4: Commit**

```bash
git add templates/agent/_claim_body.html templates/agent/partials/
git commit -m "refactor(claims): drop 240-line inline JS (replaced by htmx+alpine)"
```

---

## Phase 5 — Modest visual token refresh

### Task 11: Refine the shared flat tokens (centralized)

**Files:**
- Modify: `static/src/css/tailwind.css`
- Modify: `static/css/tailwind.css` (rebuilt)

- [ ] **Step 1: Make a small, centralized token pass**

In `static/src/css/tailwind.css`, keep flat (no glass/blur). Modest, defensible refinements only:
- Tighten `.card-modern` shadow/border for a calmer surface (it currently carries a double box-shadow — reduce to a single subtle one or none, keep the 1px border).
- Ensure consistent radius via the existing `--rounded-box`/`--rounded-btn` tokens; don't introduce new ad-hoc radii in the new partials — reuse `rounded-xl`/`card-modern`.
- Verify status-pill / badge-soft classes read calmly against `#f6f7fb`; adjust only if a color is harsh.
Keep changes small — the layout/declutter work already did the heavy lifting for "dated/messy."

- [ ] **Step 2: Rebuild and smoke-test**

Run: `npm run build && .venv/bin/python -m pytest apps/users/tests/test_page_smoke.py -o addopts=""`
Expected: build succeeds; all pages render 200.

- [ ] **Step 3: Commit**

```bash
git add static/src/css/tailwind.css static/css/tailwind.css
git commit -m "style(frontend): modest central refresh of flat tokens"
```

---

## Phase 6 — Fix the claims nav cross-wiring

### Task 12: Point the sidebar "Claims" link at the consistent list and align the back-link

**Files:**
- Modify: `templates/base.html` (sidebar "Claims" link, ~line 34)
- Test: `apps/users/tests/test_claim_nav.py`

- [ ] **Step 1: Write the failing test**

`apps/users/tests/test_claim_nav.py`:
```python
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

User = get_user_model()


class ClaimsNavConsistencyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='nav_user', password='x')
        self.web = Client()
        self.web.force_login(self.user)

    def test_sidebar_claims_link_matches_detail_back_target(self):
        # Sidebar links to the same list that claim rows return to.
        resp = self.web.get(reverse('manager_dashboard'))
        html = resp.content.decode()
        self.assertIn(reverse('agent_claims'), html,
                      "sidebar Claims should point at agent_claims (the list whose rows open agent_claim_detail)")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_nav.py -o addopts=""`
Expected: FAIL — the sidebar currently links to `manager_claims`, so `agent_claims` is not in the rendered nav.

- [ ] **Step 3: Decide and apply the consistent target**

The list whose rows open `agent_claim_detail` and whose "Back to Claims" returns there should be the one the sidebar opens. Point the sidebar "Claims" link at `agent_claims` (the role-neutral list view), and confirm the claim-detail "Back to Claims" link also targets `agent_claims`. In `templates/base.html` line ~34, change `{% url 'manager_claims' %}` to `{% url 'agent_claims' %}`.

(If, on review, the manager overview list is the preferred daily list, instead align the detail screen's back-link and row-source to `manager_claims` and update the test accordingly — but pick ONE so sidebar → list → detail → back is a closed loop.)

- [ ] **Step 4: Run to verify pass + smoke**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_nav.py apps/users/tests/test_page_smoke.py -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add templates/base.html apps/users/tests/test_claim_nav.py
git commit -m "fix(nav): sidebar Claims and claim-detail back-link target one consistent list"
```

---

## Phase 7 — Full regression gate

### Task 13: Run the whole suite and rebuild assets

- [ ] **Step 1: Rebuild CSS (ensure compiled output is current and committed)**

Run: `npm run build && git diff --stat static/css/tailwind.css`
Expected: build succeeds; if `static/css/tailwind.css` changed, it was committed in the relevant phase (no uncommitted diff here).

- [ ] **Step 2: Run the full test suite**

Run: `.venv/bin/python -m pytest -o addopts=""`
Expected: green — at or above the ~1,085-pass baseline, plus the new HTMX/nav tests. Investigate any new failure before proceeding.

- [ ] **Step 3: Final manual pass**

Re-confirm the screen end-to-end against the spec's "manual verification" list, plus: one status shown (not the old badge pile), reference cards collapsed by default, urgent-only chips, rare actions under `···`.

---

## Self-Review

**Spec coverage** (each spec requirement → task):
- Two-column layout, work left / facts right → Task 7.
- Collapse-all reference cards → Task 9 (Alpine, `open:false`).
- One status + urgent-only chips; rare actions in `···` → Task 7 (header) + Task 9 (menu).
- In-place updates via HTMX → Tasks 4, 5, 8 (fragment model: 7 form views + 5 JSON-endpoint refresh).
- Alpine for small interactions → Task 9.
- HTMX/Alpine vendored, no npm install, no runtime CDN → Task 1.
- Global CSRF for HTMX → Task 2/3.
- Remove ~240 lines inline JS → Task 10 (+ guard test Task 6).
- Refine flat styling centrally → Task 11.
- Build workflow (edit source → npm run build → commit compiled) → Tasks 3, 9, 11, 13.
- Behavior preserved; suite stays green → guard tests (Task 6) + regression gates (Tasks 5, 13).
- Nav cross-wiring fix → Task 12.
- Decluttering rules (demote IDs/JSON, hide flight detail) → Task 7 Step 2.

**Placeholder scan:** Template tasks (7-10) intentionally describe structure + which existing markup/endpoints to preserve rather than reproducing ~1,000 lines of HTML verbatim — this is presentation reorganization of existing markup, guarded by the endpoint-preservation and smoke tests (Task 6) and a manual pass (Task 10). All logic, test, JS-helper, and CSS steps contain complete code.

**Type/name consistency:** `_claim_detail_context` returns `(claim, context)` and is used by `agent_claim_detail`, `agent_claim_detail_body`, and `_claim_detail_response` consistently. Route name `agent_claim_detail_body` matches between `urls.py`, the views, the tests, and the `hx-on` refresh calls. Body root id `claim-body` matches the swap target everywhere. Helper names `refreshClaimBody`/`toast` match between `lora-htmx.js` and the template `hx-on` handlers.

**Known judgment call:** the 5 cross-app JSON endpoints keep their JSON contract and are refreshed client-side (Task 8 Step 2) rather than converted to fragment responses, to avoid coupling claims/communications/payments views to a users-app template helper. Documented; consistent within the plan.
