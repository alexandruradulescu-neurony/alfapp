# Form-fill Structured Context + Per-Site Playbooks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop sending Browser Use the raw masked ticket thread; send a clean structured form profile (with real IDs recovered) plus an editable, AI-improvable per-site playbook.

**Architecture:** A single `AIClient.complete` call structures the masked case into a `FormProfile` Pydantic object whose string fields come back untokenized (real values). Profile fields split into Browser Use *secrets* (PII / free-text) and *visible facts* (non-PII / dropdowns). A DB-backed `FormPlaybook` keyed by form domain injects site-specific instructions, editable on a backend page with an "Improve from recent runs" AI button.

**Tech Stack:** Django 5.2, DRF, Pydantic (via `AIClient`), DeepSeek (default provider for non-dispute call sites), pytest.

**Test command (this repo):** `.venv/bin/python -m pytest <path> -o addopts="" -q` (migrations must be applied first: `.venv/bin/python manage.py migrate`).

**Reference patterns:**
- `AIClient.complete` structured call + untokenize: `apps/ai/client.py:208` (signature), tokenize→untokenize is automatic. Non-`dispute_*` `call_site` ⇒ DeepSeek.
- Backend settings page pattern (view + form + template): `apps/users/views.py:1300` (save loop), `apps/config/forms.py`, `templates/manager/settings.html`.
- Current form-fill: `apps/integrations/form_fill_service.py`, `apps/integrations/views/form_fill.py`, `apps/integrations/models.py` (`FormFill`).

---

## File Structure

- Create `apps/integrations/form_profile.py` — `FormProfile` schema + `build_form_profile()` + `profile_to_secrets_and_facts()`.
- Modify `apps/integrations/form_fill_service.py` — `build_form_secrets` (accept a profile), `build_fill_task` (known-facts + playbook params; drop thread reliance).
- Modify `apps/integrations/views/form_fill.py` — `FormFillStartView` calls `build_form_profile` + playbook lookup; remove `build_agent_context` use.
- Modify `apps/claims/models.py` (+ migration) — cache `form_profile` (JSON) + `form_profile_generated_at` on `Claim`.
- Create `apps/integrations/models.py` `FormPlaybook` (+ migration).
- Create `apps/integrations/playbooks.py` — `playbook_for_domain()`, `recent_run_summaries()`, `suggest_playbook_instructions()`.
- Create manager page: view in `apps/users/views.py` (or `apps/config`), URL, `templates/manager/form_playbooks.html` (+ edit template) — mirror the settings page pattern.
- Tests: `apps/integrations/tests/test_form_profile.py`, extend `test_form_fill_service.py`, `test_form_fill_endpoints.py`, new `test_form_playbooks.py`.

`build_agent_context` in `form_fill_service.py` is **removed** once `FormFillStartView` no longer calls it (Task A5).

---

## PHASE A — Structured profile + brief rewrite (ships independently; fixes the junk + the "phone problem")

### Task A1: `FormProfile` schema + `build_form_profile`

**Files:**
- Create: `apps/integrations/form_profile.py`
- Test: `apps/integrations/tests/test_form_profile.py`

- [ ] **Step 1: Write the failing test**

```python
# apps/integrations/tests/test_form_profile.py
import pytest
from unittest.mock import patch
from apps.claims.models import Claim
from apps.integrations.form_profile import FormProfile, build_form_profile


@pytest.mark.django_db
def test_build_form_profile_returns_real_values_via_untokenize():
    claim = Claim.objects.create(
        client_email='real@e.com', client_name='Bronach Brother',
        email_alias='alias-9@mailapptoday.com', phone='+1 555 0100',
        object_description='green Eagle Creek suitcase', alf_claim_id='ALF9',
        zd_ticket_id='55')
    ticket_data = {'subject': 'ALF9', 'description': 'lost suitcase',
                   'comments': [{'author': 'client', 'public': True,
                                 'text': 'Baggage tag: 0081234567, confirmation 3BMD36'}]}
    # AIClient.complete is the boundary: it returns an ALREADY-untokenized FormProfile.
    fake = FormProfile(first_name='Bronach', last_name='Brother',
                       email_alias='alias-9@mailapptoday.com', item_type='Suitcase',
                       baggage_tag='0081234567', booking_confirmation='3BMD36',
                       airport='Newark Liberty / EWR')
    with patch('apps.integrations.form_profile.AIClient') as Client:
        Client.return_value.complete.return_value = fake
        profile = build_form_profile(claim, ticket_data)
    assert profile.baggage_tag == '0081234567'        # recovered ID, not a <PHONE_..> mask
    assert profile.booking_confirmation == '3BMD36'
    assert profile.item_type == 'Suitcase'
    # the call used a non-dispute call_site (=> DeepSeek) and passed a response_schema
    kwargs = Client.return_value.complete.call_args.kwargs
    assert kwargs['response_schema'] is FormProfile
    assert not kwargs['call_site'].startswith('dispute')


@pytest.mark.django_db
def test_build_form_profile_returns_none_on_ai_failure():
    claim = Claim.objects.create(client_email='c@e.com', alf_claim_id='ALF1', zd_ticket_id='55')
    with patch('apps.integrations.form_profile.AIClient') as Client:
        Client.return_value.complete.side_effect = RuntimeError('boom')
        assert build_form_profile(claim, {'comments': []}) is None   # caller falls back
```

- [ ] **Step 2: Run, verify it fails**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_profile.py -o addopts="" -q`
Expected: FAIL (`ModuleNotFoundError: apps.integrations.form_profile`).

- [ ] **Step 3: Implement `form_profile.py`**

```python
"""Turn a messy, masked ticket into a clean structured form profile. One AIClient
call: the model only ever sees masked text; AIClient.complete returns the validated
FormProfile with every string field UNTOKENIZED, so real values (baggage tag, marks)
are recovered server-side. Non-dispute call_site => DeepSeek (cheap)."""
import logging
from pydantic import BaseModel
from apps.ai.client import AIClient
from apps.integrations.briefing import normalize_fetched_comments
from apps.integrations.services import build_ticket_thread

logger = logging.getLogger(__name__)


class FormProfile(BaseModel):
    # claimant (PII -> secrets)
    first_name: str = ''
    last_name: str = ''
    email_alias: str = ''
    phone: str = ''
    # item
    item_type: str = ''           # visible (dropdowns)
    item_description: str = ''    # secret (full clean description incl. brand/colour/marks)
    # loss
    airport: str = ''             # visible
    airline: str = ''             # visible
    flight: str = ''              # secret (masked category)
    lost_date: str = ''           # visible (MM/DD/YYYY if known)
    where_lost: str = ''          # visible (short category)
    how_lost: str = ''            # secret (incident narrative)
    # ids (secrets)
    baggage_tag: str = ''
    booking_confirmation: str = ''
    claim_ref: str = ''
    # address
    street: str = ''              # secret
    city: str = ''                # visible
    state: str = ''               # visible
    zip: str = ''                 # visible
    country: str = ''             # visible


SYSTEM_PROMPT = (
    "You extract a structured lost-item form profile from an airport lost-and-found case. "
    "Return ONLY the fields you can support from the case text; leave the rest blank. "
    "item_description: one tidy sentence with brand, colour and identifying marks. "
    "how_lost: one tidy sentence on the circumstances. lost_date as MM/DD/YYYY if stated. "
    "Do not invent values. Copy any <NAME_..>/<PHONE_..>/<ALIAS_..> placeholders verbatim "
    "into the matching field — they are resolved later."
)


def _case_text(claim, ticket_data: dict) -> str:
    thread = build_ticket_thread({
        'subject': ticket_data.get('subject', ''),
        'description': ticket_data.get('description', ''),
        'ticket_created_at': ticket_data.get('created_at', '') or ticket_data.get('ticket_created_at', ''),
        'comments': normalize_fetched_comments(ticket_data.get('comments', [])),
    })
    parts = []
    if thread.get('ticket_subject'):
        parts.append('Subject: ' + thread['ticket_subject'])
    if thread.get('ticket_description'):
        parts.append('Description: ' + thread['ticket_description'])
    parts.extend(thread.get('zendesk_comment', []))
    return '\n'.join(parts).strip()


def build_form_profile(claim, ticket_data: dict):
    """Return a FormProfile (real values) or None on failure (caller falls back)."""
    case = _case_text(claim, ticket_data)
    if not case:
        return None
    known_pii = {
        'aliases': [a for a in [getattr(claim, 'email_alias', ''),
                                getattr(claim, 'client_email', '')] if a],
        'names': [n for n in [getattr(claim, 'client_name', '')] if n],
    }
    try:
        return AIClient().complete(
            system_prompt=SYSTEM_PROMPT,
            trusted={'case': case},
            known_pii=known_pii,
            response_schema=FormProfile,
            call_site='form_fill_profile',
        )
    except Exception as e:                       # noqa: BLE001 — never block a fill
        logger.warning('Form profile build failed for claim %s: %s', claim.pk, e)
        return None
```

- [ ] **Step 4: Run, verify it passes**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_form_profile.py -o addopts="" -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/integrations/form_profile.py apps/integrations/tests/test_form_profile.py
git commit -m "feat(form-fill): structure the masked case into a FormProfile (real IDs via untokenize)"
```

### Task A2: split a profile into secrets + visible facts

**Files:**
- Modify: `apps/integrations/form_profile.py` (add `profile_to_secrets_and_facts`)
- Test: `apps/integrations/tests/test_form_profile.py`

- [ ] **Step 1: Write the failing test**

```python
def test_profile_split_keeps_pii_in_secrets_and_facts_clean():
    from apps.integrations.form_profile import FormProfile, profile_to_secrets_and_facts
    p = FormProfile(first_name='Bronach', last_name='Brother',
                    email_alias='alias-9@mailapptoday.com', phone='+15550100',
                    item_type='Suitcase', item_description='green Eagle Creek bag, tag "Bronach"',
                    airport='Newark Liberty / EWR', state='IL', baggage_tag='0081234567',
                    street='7509 N Pecatonica Rd')
    secrets, facts = profile_to_secrets_and_facts(p)
    # PII / IDs / free-text -> secrets
    assert secrets['x_client_first_name'] == 'Bronach'
    assert secrets['x_client_email'] == 'alias-9@mailapptoday.com'
    assert secrets['x_baggage_tag'] == '0081234567'
    assert secrets['x_item_description'].startswith('green Eagle Creek')
    assert secrets['x_street_address'] == '7509 N Pecatonica Rd'
    # non-PII dropdown facts -> visible
    assert facts['Item type'] == 'Suitcase'
    assert facts['Airport'] == 'Newark Liberty / EWR'
    assert facts['State'] == 'IL'
    # the personal values must NOT appear in the visible facts
    blob = ' '.join(facts.values())
    assert 'Bronach' not in blob and 'alias-9@mailapptoday.com' not in blob and '0081234567' not in blob
    # masks never leak into secrets
    assert not any(str(v).startswith('<') for v in secrets.values())
```

- [ ] **Step 2: Run, verify it fails** — `ImportError: profile_to_secrets_and_facts`.

- [ ] **Step 3: Implement**

```python
# secret placeholder -> FormProfile attr (real values typed verbatim into free-text fields)
_SECRET_FIELDS = [
    ('x_client_first_name', 'first_name'), ('x_client_last_name', 'last_name'),
    ('x_client_email', 'email_alias'), ('x_client_phone', 'phone'),
    ('x_item_description', 'item_description'), ('x_incident_details', 'how_lost'),
    ('x_flight_details', 'flight'), ('x_baggage_tag', 'baggage_tag'),
    ('x_booking_ref', 'booking_confirmation'), ('x_claim_ref', 'claim_ref'),
    ('x_street_address', 'street'),
]
# label -> attr (non-PII; shown to the agent so it can pick dropdowns)
_VISIBLE_FIELDS = [
    ('Item type', 'item_type'), ('Airport', 'airport'), ('Airline', 'airline'),
    ('Date of loss', 'lost_date'), ('Where lost', 'where_lost'),
    ('City', 'city'), ('State', 'state'), ('Zip', 'zip'), ('Country', 'country'),
]


def profile_to_secrets_and_facts(profile):
    """(secrets {x_*: real}, facts {Label: real}). A '<...>' value (untokenize miss)
    is dropped from secrets so a mask never reaches the form."""
    secrets = {}
    for key, attr in _SECRET_FIELDS:
        val = str(getattr(profile, attr, '') or '').strip()
        if val and not val.startswith('<'):
            secrets[key] = val
    facts = {}
    for label, attr in _VISIBLE_FIELDS:
        val = str(getattr(profile, attr, '') or '').strip()
        if val and not val.startswith('<'):
            facts[label] = val
    return secrets, facts
```

- [ ] **Step 4: Run, verify it passes.**
- [ ] **Step 5: Commit** — `git commit -am "feat(form-fill): split profile into PII secrets vs visible dropdown facts"`

### Task A3: `build_fill_task` accepts known-facts + a playbook string

**Files:**
- Modify: `apps/integrations/form_fill_service.py` (`build_fill_task` signature + body)
- Test: `apps/integrations/tests/test_form_fill_service.py`

- [ ] **Step 1: Write the failing test**

```python
def test_fill_task_includes_facts_and_playbook_and_keeps_safety_rules():
    secrets = {'lf.example': {'x_client_first_name': 'F', 'x_item_description': 'D',
                              'x_baggage_tag': 'T'}}
    facts = {'Item type': 'Suitcase', 'Airport': 'EWR'}
    task = build_fill_task('https://lf.example/r', secrets, facts=facts,
                           playbook='Item type is a pop-up picker; type to select.')
    low = task.lower()
    assert 'item type: suitcase' in low and 'airport: ewr' in low      # visible facts
    assert 'x_baggage_tag' in task                                     # new secret label
    assert 'pop-up picker' in low                                      # site playbook injected
    assert 'never type a masked placeholder' in low                    # safety kept
    assert 'do not submit' in low
    assert 'F' not in task and 'D' not in task                         # real secret values never inline
```

- [ ] **Step 2: Run, verify it fails** (TypeError: unexpected `facts`/`playbook`).

- [ ] **Step 3: Implement** — change the signature to `build_fill_task(url, secrets, facts=None, playbook='', context='')` and build the brief from: role+url → a "Known facts about this case" block from `facts` → the secret-key label list (reuse `_LABELS`; add labels for `x_baggage_tag`="the baggage tag number", `x_booking_ref`="the booking/confirmation number", `x_street_address`="the shipping street address") → the playbook text (if any, under "Site-specific guidance:") → the existing safety rules block (keep verbatim: never type a `<…>` token; do NOT invent/infer; skip fiddly control after two tries; do NOT submit). Keep the optional `context` preamble param for back-compat but pass `''` from the view. Keep `build_form_secrets(claim, host)` as the fallback path.

- [ ] **Step 4: Run** the full service test file; Expected: PASS (existing + new).
- [ ] **Step 5: Commit** — `git commit -am "feat(form-fill): brief = known facts + secret labels + site playbook + safety rules"`

### Task A4: cache the profile on `Claim`

**Files:**
- Modify: `apps/claims/models.py` (add `form_profile = JSONField(default=dict, blank=True)`, `form_profile_generated_at = DateTimeField(null=True, blank=True)`)
- Migration: `apps/claims/migrations/` (autogenerate)
- Test: covered via the endpoint test in A5 (no standalone unit test needed; it's two nullable columns).

- [ ] **Step 1:** Add the two fields after the existing `flight_data` fields in `Claim`.
- [ ] **Step 2:** `.venv/bin/python manage.py makemigrations claims` then `migrate`. Expected: one new migration adding two fields.
- [ ] **Step 3: Commit** — `git add -A && git commit -m "feat(claims): cache the form-fill profile on the claim"`

### Task A5: wire the profile + facts into `FormFillStartView`; remove `build_agent_context`

**Files:**
- Modify: `apps/integrations/views/form_fill.py` (`FormFillStartView.post`)
- Modify: `apps/integrations/form_fill_service.py` (delete `build_agent_context`)
- Test: `apps/integrations/tests/test_form_fill_endpoints.py`

- [ ] **Step 1: Write the failing test** (extend the start endpoint test)

```python
@pytest.mark.django_db
def test_start_uses_structured_profile_secrets_and_facts(api, settings_obj):
    claim = Claim.objects.create(client_email='real@e.com', client_name='Bronach Brother',
                                 zd_ticket_id='55', alf_claim_id='ALF1',
                                 email_alias='alias-55@mailapptoday.com')
    from apps.integrations.form_profile import FormProfile
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
    assert 'Item type: Suitcase' in task                     # visible fact in the brief
    assert 'Bronach' not in task                             # PII not inline in the brief
```

- [ ] **Step 2: Run, verify it fails** (no `build_form_profile` imported in the view; facts not in task).

- [ ] **Step 3: Implement** — in `FormFillStartView.post`: on a retry (an existing `form_fill_id` and a populated `claim.form_profile`) rebuild `profile = FormProfile(**claim.form_profile)` instead of re-calling the AI (don't re-pay); otherwise `profile = build_form_profile(claim, ticket_data)`. If profile, `secrets_map, facts = profile_to_secrets_and_facts(profile)` and `secrets = {host: secrets_map}`, and cache `claim.form_profile = profile.model_dump()` + `form_profile_generated_at = now()`; else fall back to `build_form_secrets(claim, host)` with `facts = {}`. Look up `playbook = playbook_for_domain(host)` (Phase B; until then pass `''`). Call `build_fill_task(url, secrets, facts=facts, playbook=playbook)`. Remove the `build_agent_context` import/call. Add imports for `build_form_profile`, `profile_to_secrets_and_facts`.

- [ ] **Step 4: Run** `test_form_fill_endpoints.py` + `test_form_fill_service.py`; Expected: PASS. Then delete `build_agent_context` and its now-dead test (`test_build_agent_context_*`), re-run.
- [ ] **Step 5: Commit** — `git commit -am "feat(form-fill): send the structured profile + facts to Browser Use; drop the raw thread"`

---

## PHASE B — Per-site playbook (DB) + backend page

### Task B1: `FormPlaybook` model + migration

**Files:** Modify `apps/integrations/models.py`; autogenerate migration; Test `apps/integrations/tests/test_form_playbooks.py`.

- [ ] **Step 1: Failing test**

```python
import pytest
from apps.integrations.models import FormPlaybook
from apps.integrations.playbooks import playbook_for_domain


@pytest.mark.django_db
def test_playbook_lookup_by_domain_respects_enabled():
    FormPlaybook.objects.create(domain='chargerback.com', label='Chargerback',
                                instructions='Type item type into the picker.', enabled=True)
    FormPlaybook.objects.create(domain='off.example', label='Off', instructions='x', enabled=False)
    assert 'picker' in playbook_for_domain('chargerback.com')
    assert playbook_for_domain('off.example') == ''       # disabled => no instructions
    assert playbook_for_domain('unknown.com') == ''
```

- [ ] **Step 2: Run, verify it fails** (model/module missing).
- [ ] **Step 3: Implement** the model (`domain` unique + lowercased on save, `label`, `instructions` TextField, `enabled` bool default True, `created_at`/`updated_at` auto) and `apps/integrations/playbooks.py::playbook_for_domain(domain)` returning `instructions` for an enabled match else `''`. `makemigrations integrations && migrate`.
- [ ] **Step 4: Run, verify it passes.**
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(form-fill): FormPlaybook model + per-domain lookup"`

### Task B2: inject the playbook at fill time

**Files:** Modify `apps/integrations/views/form_fill.py`; Test `test_form_fill_endpoints.py`.

- [ ] **Step 1: Failing test** — a `FormPlaybook(domain='lf.example', instructions='Tick both consent boxes.')` exists; assert `m.call_args[1]['task']` contains `'Tick both consent boxes.'`.
- [ ] **Step 2: Run, verify it fails.**
- [ ] **Step 3: Implement** — in `FormFillStartView.post` set `playbook = playbook_for_domain(host)` and pass it to `build_fill_task`. Import `playbook_for_domain`.
- [ ] **Step 4: Run, verify it passes.**
- [ ] **Step 5: Commit** — `git commit -am "feat(form-fill): inject the site playbook into the brief"`

### Task B3: backend Form-playbooks page (list / create / edit / delete)

**Files:** Modify `apps/users/views.py` (+ URL in the manager urls), Create `templates/manager/form_playbooks.html`; Test `test_form_playbooks.py`.

Follow the existing manager-page pattern (auth decorator/mixin used by the settings view; `messages`; POST-save). One page lists playbooks and edits/creates them (domain, label, instructions textarea, enabled checkbox); a delete action.

- [ ] **Step 1: Failing test** — authed GET renders 200 and lists an existing playbook's label; authed POST with `{domain, label, instructions, enabled}` creates/updates the row (assert DB); unauth GET redirects/403.
- [ ] **Step 2: Run, verify it fails.**
- [ ] **Step 3: Implement** the view + URL + template (mirror `settings` view/template). Use a `ModelForm` for `FormPlaybook` or explicit field handling like the settings save loop.
- [ ] **Step 4: Run, verify it passes.**
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(form-fill): backend Form playbooks page (CRUD)"`

---

## PHASE C — AI "Improve from recent runs"

### Task C1: gather recent run summaries for a domain

**Files:** Modify `apps/integrations/playbooks.py` (`recent_run_summaries(domain, limit=5)`); Test `test_form_playbooks.py`.

- [ ] **Step 1: Failing test** — create FormFills whose `form_url` host = the domain with non-empty `result_output`; assert `recent_run_summaries('chargerback.com')` returns those summaries newest-first, capped at `limit`, excluding empty ones and other domains.
- [ ] **Step 2: Run, verify it fails.**
- [ ] **Step 3: Implement** — filter `FormFill` by `form_url__icontains` the domain (or compute host), `exclude(result_output='')`, order by `-updated_at`, `[:limit]`, return the `result_output` strings.
- [ ] **Step 4: Run, verify it passes.**
- [ ] **Step 5: Commit** — `git commit -am "feat(form-fill): collect recent run summaries per domain"`

### Task C2: `suggest_playbook_instructions`

**Files:** Modify `apps/integrations/playbooks.py`; Test `test_form_playbooks.py`.

- [ ] **Step 1: Failing test**

```python
def test_suggest_playbook_instructions_calls_ai_and_returns_text():
    from unittest.mock import patch
    from apps.integrations.playbooks import suggest_playbook_instructions, _Suggestion
    with patch('apps.integrations.playbooks.AIClient') as Client:
        Client.return_value.complete.return_value = _Suggestion(instructions='Updated tips.')
        out = suggest_playbook_instructions(current='old tips', summaries=['run said: picker is fiddly'])
    assert out == 'Updated tips.'
    assert not Client.return_value.complete.call_args.kwargs['call_site'].startswith('dispute')
```

- [ ] **Step 2: Run, verify it fails.**
- [ ] **Step 3: Implement** — a `_Suggestion(BaseModel)` with `instructions: str = ''`; `suggest_playbook_instructions(current, summaries)` calls `AIClient().complete(system_prompt=..., trusted={'current': current, 'runs': '\n---\n'.join(summaries)}, response_schema=_Suggestion, call_site='form_fill_playbook_suggest')` and returns `.instructions`; return `''` on empty `summaries` or exception. System prompt: "You improve a per-site form-filling instruction set from real run reports. Output concise, imperative, site-specific tips (dropdowns, pickers, required checkboxes, what to leave blank). Keep what still applies; add what the runs revealed."
- [ ] **Step 4: Run, verify it passes.**
- [ ] **Step 5: Commit** — `git commit -am "feat(form-fill): AI drafts playbook instructions from recent runs"`

### Task C3: "Improve from recent runs" button + endpoint

**Files:** Modify the Form-playbooks view/template (Task B3) to add a button that POSTs to a new action; the action calls `recent_run_summaries(domain)` → `suggest_playbook_instructions(...)` and returns the draft into the instructions textarea (no auto-save). Test in `test_form_playbooks.py`.

- [ ] **Step 1: Failing test** — authed POST to the suggest action for a domain (with `suggest_playbook_instructions` patched) returns 200 and the draft text is present in the response/context; the saved row is unchanged (no auto-save).
- [ ] **Step 2: Run, verify it fails.**
- [ ] **Step 3: Implement** — a view action (separate URL or a `?action=suggest` branch) that renders the edit page with the draft pre-filled in the textarea for review; a visible note "Review and Save to apply." Respect CSP (no eval; plain form POST or a simple `onclick` submit).
- [ ] **Step 4: Run, verify it passes.**
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(form-fill): Improve-from-recent-runs button on the playbook page"`

---

## Final verification (after all phases)

- [ ] Run the full touched suites: `.venv/bin/python -m pytest apps/integrations/tests/ apps/claims/tests/ -o addopts="" -q`
- [ ] `.venv/bin/python manage.py check`
- [ ] Confirm `build_agent_context` is gone and nothing imports it: `grep -rn build_agent_context apps/`

## Out of scope (per spec)
Deep step-by-step run capture; structured per-field rule rows; auto-applying AI suggestions. All deferred.
