# Claim Risk Flag — Part B: Delta-Aware Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.
>
> **Project TDD note (strict, blind):** tests are authored from the behavioral spec BEFORE and WITHOUT sight of the implementation; the RED failure is watched first; a test found wrong during GREEN is aborted back to RED, never patched to match the code. The test code in each task is the contract to author from. **Implementers must NOT weaken a test to make it pass** — if a test looks wrong, STOP and report BLOCKED.

**Goal:** Make each `ClaimUpdateTimeline` entry describe WHAT CHANGED (a delta) instead of re-narrating the whole claim, suppress no-op manual refreshes, and stop the status-name's canned meaning from contradicting the facts. The stored `claim.ai_summary` snapshot stays a full current-state summary (unchanged).

**Architecture:** Fold the delta into the EXISTING summary AI pass — `BriefingSummary` gains a `delta` field and `generate_claim_summary`/`refresh_claim_summary` take a `previous_note` so the model writes "what's new since the previous update" in the same call (no extra LLM call). `refresh_claim_summary` returns that delta string (never empty on success → backward-compatible with `if refresh_claim_summary(...)` callers). The status webhook stores a deterministic transition headline + the delta; the manual-refresh path stores a deterministic "Updated: <fields>" line and is suppressed when nothing changed. The summary prompt is told to defer to the facts/thread over the status label's implied meaning.

**Tech Stack:** Django 5.2, Pydantic schemas (`apps/ai/schemas.py`), pytest. Run tests with `.venv/bin/python -m pytest <path> -o addopts="" -q` (`python` is not on PATH).

**Reference spec:** `docs/superpowers/specs/2026-06-18-claim-risk-flag-and-delta-timeline-design.md` (Part B).

---

## File structure
- **Modify** `apps/ai/schemas.py` — add `delta` to `BriefingSummary`.
- **Modify** `apps/integrations/briefing.py` — thread `previous_note` into `generate_claim_summary`; add the delta instruction + defer-to-facts (boilerplate) fix to `SUMMARY_PROMPT`; make `refresh_claim_summary(previous_note='')` return `Optional[str]` (the delta).
- **Modify** `apps/integrations/views/webhooks.py` — `mirror_status_change`: capture the previous timeline note, build a deterministic transition headline, store headline + delta in the entry's `llm_summary` (delta from `refresh_claim_summary`; deterministic-only fallback if the AI pass fails).
- **Modify** `apps/claims/views.py` — `ClaimUpdateFromZendeskView.post`: suppress the INFO_UPDATED row when no fields changed; store a deterministic "Updated: <fields>" line otherwise.
- **Create** tests: `apps/integrations/tests/test_delta_timeline.py`, and additions to `apps/claims/tests/` for the manual-refresh path.

---

## Task B1: `delta` in the summary pass + `previous_note` + boilerplate fix

**Files:**
- Modify: `apps/ai/schemas.py` (`BriefingSummary`)
- Modify: `apps/integrations/briefing.py` (`SUMMARY_PROMPT`, `generate_claim_summary`, `refresh_claim_summary`)
- Create: `apps/integrations/tests/test_delta_timeline.py`

- [ ] **Step 1: Write failing tests** — `apps/integrations/tests/test_delta_timeline.py`:

```python
from unittest.mock import patch
from django.test import TestCase
from apps.claims.models import Claim
from apps.integrations import briefing
from apps.ai.schemas import BriefingSummary


def _claim(**kw):
    base = dict(client_email='c@example.com', zd_ticket_id='95001', alf_claim_id='ALF9500001')
    base.update(kw)
    return Claim.objects.create(**base)


def _fake_result(summary='Full current-state summary.', delta='Item located at BOS.',
                 risk_level='none', risk_reasons=None, risk_note=''):
    return BriefingSummary(summary=summary, delta=delta, risk_level=risk_level,
                           risk_reasons=risk_reasons or [], risk_note=risk_note)


class RefreshReturnsDeltaTests(TestCase):
    def test_returns_delta_and_stores_full_snapshot(self):
        c = _claim()
        with patch.object(briefing, 'generate_claim_summary', return_value=_fake_result()):
            delta = briefing.refresh_claim_summary(c, {'subject': '', 'comments': []},
                                                   previous_note='earlier note')
        self.assertEqual(delta, 'Item located at BOS.')
        c.refresh_from_db()
        self.assertEqual(c.ai_summary, 'Full current-state summary.')   # snapshot stays FULL

    def test_empty_delta_coerced_to_no_new_information(self):
        c = _claim()
        with patch.object(briefing, 'generate_claim_summary', return_value=_fake_result(delta='')):
            delta = briefing.refresh_claim_summary(c, {'subject': '', 'comments': []})
        self.assertEqual(delta, 'No new information.')   # success is never empty/falsy

    def test_ai_failure_returns_none_and_leaves_snapshot(self):
        c = _claim(ai_summary='OLD')
        with patch.object(briefing, 'generate_claim_summary', return_value=None):
            delta = briefing.refresh_claim_summary(c, {'subject': '', 'comments': []})
        self.assertIsNone(delta)
        c.refresh_from_db()
        self.assertEqual(c.ai_summary, 'OLD')

    def test_previous_note_is_passed_to_generation(self):
        c = _claim()
        with patch.object(briefing, 'generate_claim_summary', return_value=_fake_result()) as gen:
            briefing.refresh_claim_summary(c, {'subject': '', 'comments': []}, previous_note='PRIOR')
        # previous_note forwarded to generate_claim_summary (kw or positional)
        _, kwargs = gen.call_args
        self.assertEqual(kwargs.get('previous_note', (gen.call_args[0][2:3] or [''])[0]), 'PRIOR')


class PromptDeferToFactsTests(TestCase):
    def test_summary_prompt_tells_model_to_defer_to_facts(self):
        # boilerplate-contradiction fix: the status label's meaning must not override the thread
        self.assertIn('defer to', briefing.SUMMARY_PROMPT.lower())

    def test_briefing_summary_schema_has_delta_default(self):
        bs = BriefingSummary(summary='x')
        self.assertEqual(bs.delta, '')
```

- [ ] **Step 2: Run, verify FAIL**

`.venv/bin/python -m pytest apps/integrations/tests/test_delta_timeline.py::RefreshReturnsDeltaTests apps/integrations/tests/test_delta_timeline.py::PromptDeferToFactsTests -o addopts="" -q`
Expected: FAIL — `BriefingSummary` has no `delta`; `refresh_claim_summary` returns a bool and takes no `previous_note`; prompt lacks the defer phrase.

- [ ] **Step 3: Add `delta` to `BriefingSummary`** (`apps/ai/schemas.py`), beside the Part A risk fields:
```python
    delta: str = Field(default='', max_length=400)
```

- [ ] **Step 4: Update `SUMMARY_PROMPT` and the two functions** (`apps/integrations/briefing.py`)

Append to `SUMMARY_PROMPT` (after the risk instructions, before/with the JSON line) BOTH the delta instruction and the defer-to-facts fix, and add `delta` to the JSON example:
```python
    "\n\nThe status vocabulary above explains what each status NAME means — use it only to "
    "interpret the current label. ALWAYS defer to the actual claim facts and ticket thread for "
    "what has happened; do NOT assert process steps (e.g. whether loss reports were filed) from "
    "the status name if the thread shows otherwise.\n"
    "Also produce `delta`: 1-2 sentences on what is NEW since the previous update note below. "
    "If nothing material has changed beyond any status transition, respond with exactly "
    "'No new information.'. Previous update note: {previous_note}\n"
    'Respond as JSON: {"summary": "...", "delta": "...", "risk_level": "...", '
    '"risk_reasons": [...], "risk_note": "..."}.'
```
IMPORTANT: `SUMMARY_PROMPT` is a module constant. The `{previous_note}` placeholder must be substituted per-call, NOT via `str.format` on the whole constant (the prompt contains literal `{...}` JSON braces that would break `.format`). Instead, pass `previous_note` through the `trusted` channel (see below) and phrase the prompt to reference "the previous update note in the provided context". Concretely: DROP the literal `{previous_note}` from the constant and instead say "Previous update note is provided in the context." Then pass it in `trusted`.

`generate_claim_summary` — add `previous_note: str = ''` and include it in the trusted context:
```python
def generate_claim_summary(claim, ticket_data, previous_note: str = ''):
    ...
    result = AIClient.complete(
        system_prompt=SUMMARY_PROMPT,
        trusted={'claim_facts': str(facts), 'previous_update_note': previous_note or '(none)'},
        untrusted=untrusted,
        known_pii=known_pii,
        response_schema=BriefingSummary,
        call_site='claim_summary',
        temperature=SUMMARY_TEMPERATURE,
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    return result
```

`refresh_claim_summary` — add `previous_note` and RETURN the delta (never empty on success; None on failure):
```python
def refresh_claim_summary(claim, ticket_data, previous_note: str = '') -> 'Optional[str]':
    from django.utils import timezone
    result = generate_claim_summary(claim, ticket_data, previous_note=previous_note)
    if result is None:
        return None
    claim.ai_summary = result.summary.strip()        # snapshot stays FULL
    claim.ai_summary_updated_at = timezone.now()
    claim.save(update_fields=['ai_summary', 'ai_summary_updated_at', 'updated_at'])
    # ... existing Part A risk wiring (merge_risk + claim.register_risk) UNCHANGED ...
    return (result.delta or '').strip() or 'No new information.'
```
Keep the existing Part A risk block exactly as it is; only the signature, the `previous_note` forwarding, and the return changed. Confirm no caller does `refresh_claim_summary(...) is True` / `== True` (a truthy string still satisfies `if x:`); the recon found only `if`/truthiness callers — verify by grepping.

- [ ] **Step 5: Run, verify PASS**

`.venv/bin/python -m pytest apps/integrations/tests/test_delta_timeline.py -o addopts="" -q` → PASS.

- [ ] **Step 6: Integrations regression**

`.venv/bin/python -m pytest apps/integrations/ apps/claims/ -o addopts="" -q` → all pass. Fix only a PRE-EXISTING test that asserted the OLD `refresh_claim_summary` bool return (now a string/None — a truthy success string still passes `if`; only a literal `is True`/`assertEqual(..., True)` would break, which is a legitimate contract update). Do NOT weaken new tests. `.venv/bin/python manage.py check` clean.

- [ ] **Step 7: Commit**

```bash
git add apps/ai/schemas.py apps/integrations/briefing.py apps/integrations/tests/test_delta_timeline.py
git commit -m "feat(briefing): delta field + previous_note in the summary pass; defer-to-facts prompt fix"
```

---

## Task B2: Status-change timeline entry = transition headline + delta

**Files:**
- Modify: `apps/integrations/views/webhooks.py` (`mirror_status_change`)
- Test: add to `apps/integrations/tests/test_delta_timeline.py`

- [ ] **Step 1: Write failing tests** (append):

```python
from apps.integrations.views import webhooks
from apps.claims.models import ClaimUpdateTimeline


class StatusEntryDeltaTests(TestCase):
    def _run(self, old_status, old_cat, new_name, new_cat, delta_return):
        c = _claim(zd_ticket_id='95100', alf_claim_id='ALF9510000',
                   status=old_status, status_category=old_cat, ai_summary='FULL SNAPSHOT TEXT')
        with patch('apps.integrations.views.webhooks.resolve_custom_status',
                   return_value={'name': new_name, 'category': new_cat}), \
             patch('apps.integrations.views.webhooks.fetch_zendesk_ticket', return_value={'subject': 's'}), \
             patch('apps.integrations.views.webhooks.fetch_zendesk_comments', return_value=[]), \
             patch('apps.integrations.views.webhooks.refresh_claim_summary', return_value=delta_return):
            webhooks.mirror_status_change(c, custom_status_id='123')
        return c, c.updates.first()   # newest entry

    def test_entry_shows_transition_and_delta_not_full_summary(self):
        c, entry = self._run('Claim submitted', 'open', 'Solved', 'solved', 'Item located at BOS.')
        self.assertIn('Claim submitted', entry.llm_summary)
        self.assertIn('Solved', entry.llm_summary)
        self.assertIn('Item located at BOS.', entry.llm_summary)
        self.assertNotIn('FULL SNAPSHOT TEXT', entry.llm_summary)   # NOT the parroted snapshot

    def test_regression_marked_in_entry(self):
        c, entry = self._run('Solved', 'solved', 'Investigation initiated', 'open', 'No new information.')
        self.assertIn('Investigation initiated', entry.llm_summary)
        self.assertIn('reopened', entry.llm_summary.lower())

    def test_ai_failure_falls_back_to_transition_only(self):
        c, entry = self._run('Claim submitted', 'open', 'Solved', 'solved', None)  # AI failed
        self.assertIn('Solved', entry.llm_summary)         # transition still present
        self.assertNotEqual(entry.llm_summary.strip(), '')  # never blank
```

- [ ] **Step 2: Run, verify FAIL** (`entry.llm_summary` currently = full `claim.ai_summary`).

`.venv/bin/python -m pytest apps/integrations/tests/test_delta_timeline.py::StatusEntryDeltaTests -o addopts="" -q`

- [ ] **Step 3: Implement in `mirror_status_change`**

Before the atomic block (where `old_status`/`old_category` are captured), also capture the previous note:
```python
        previous_note = (claim.updates.first().llm_summary if claim.updates.exists() else '')
```
Replace the back-fill block (currently `if refresh_claim_summary(...): entry.llm_summary = claim.ai_summary; entry.save(...)`) with:
```python
        regressed = old_category == 'solved' and resolved['category'] != 'solved'
        transition = f"Status: {old_status or '—'} → {new_status}" + (" (reopened)" if regressed else "")
        delta = None
        ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
        if ticket_data:
            ticket_data['comments'] = fetch_zendesk_comments(claim.zd_ticket_id)
            delta = refresh_claim_summary(claim, ticket_data, previous_note=previous_note)
        entry.llm_summary = f"{transition}. {delta}" if delta else transition
        entry.save(update_fields=['llm_summary'])
```
This reuses the `regressed` computation that the Part A status-regression block already does — keep the Part A `register_risk(['status_regression'], ...)` call (it can share the same `regressed` boolean). The deterministic `transition` guarantees the entry is never blank even when the AI pass fails.

- [ ] **Step 4: Run, verify PASS**; then `.venv/bin/python -m pytest apps/integrations/ -o addopts="" -q` (all pass).

- [ ] **Step 5: Commit**

```bash
git add apps/integrations/views/webhooks.py apps/integrations/tests/test_delta_timeline.py
git commit -m "feat(webhooks): timeline entry shows status transition + delta, not the full summary"
```

---

## Task B3: Manual refresh — suppress no-op, deterministic "Updated: <fields>"

**Files:**
- Modify: `apps/claims/views.py` (`ClaimUpdateFromZendeskView.post`)
- Create: `apps/claims/tests/test_refresh_timeline_delta.py`

- [ ] **Step 1: Write failing tests** — `apps/claims/tests/test_refresh_timeline_delta.py`:

```python
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from apps.claims.models import Claim, ClaimUpdateTimeline

User = get_user_model()


class ManualRefreshTimelineTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='u', password='x')
        self.client.force_login(self.user)
        self.claim = Claim.objects.create(client_email='c@example.com', zd_ticket_id='95200',
                                          alf_claim_id='ALF9520000')

    def _post(self):
        return self.client.post(f'/api/claims/{self.claim.id}/update-from-zendesk/')

    @patch('apps.claims.views.fetch_zendesk_ticket', return_value={'subject': 's'})
    @patch('apps.claims.views.fetch_zendesk_comments', return_value=[])
    @patch('apps.claims.views.analyze_zendesk_ticket_for_claim', return_value={})
    @patch('apps.claims.views.refresh_claim_summary', return_value='No new information.')
    def test_no_change_writes_no_timeline_row(self, *_m):
        with patch('apps.claims.views.refresh_claim_from_zendesk', return_value=[]):
            self._post()
        self.assertEqual(ClaimUpdateTimeline.objects.filter(claim=self.claim).count(), 0)

    @patch('apps.claims.views.fetch_zendesk_ticket', return_value={'subject': 's'})
    @patch('apps.claims.views.fetch_zendesk_comments', return_value=[])
    @patch('apps.claims.views.analyze_zendesk_ticket_for_claim', return_value={})
    @patch('apps.claims.views.refresh_claim_summary', return_value='No new information.')
    def test_changed_fields_write_deterministic_row(self, *_m):
        with patch('apps.claims.views.refresh_claim_from_zendesk', return_value=['phone', 'shipping_address']):
            self._post()
        entry = ClaimUpdateTimeline.objects.get(claim=self.claim)
        self.assertEqual(entry.update_type, 'INFO_UPDATED')
        self.assertIn('phone', entry.llm_summary.lower())
        self.assertIn('shipping', entry.llm_summary.lower())
        self.assertNotIn('No new information', entry.llm_summary)  # deterministic field line, not the AI delta
```

(Confirm the patch targets are the names imported INTO `apps/claims/views.py`; adjust if an import alias differs. Confirm the update-from-zendesk URL path.)

- [ ] **Step 2: Run, verify FAIL** (a row is created unconditionally today; its body is the full summary).

`.venv/bin/python -m pytest apps/claims/tests/test_refresh_timeline_delta.py -o addopts="" -q`

- [ ] **Step 3: Implement in `ClaimUpdateFromZendeskView.post`**

Keep calling `refresh_claim_summary` (snapshot + Part A risk still run on every manual refresh, even a no-op — risk can hide in a new comment with no field change). Change only the timeline-row creation to be conditional + deterministic:
```python
        with transaction.atomic():
            updated_fields = refresh_claim_from_zendesk(claim, extracted)
            if updated_fields:
                pretty = ", ".join(f.replace('_', ' ') for f in updated_fields)
                ClaimUpdateTimeline.objects.create(
                    claim=claim,
                    zendesk_ticket_id=claim.zd_ticket_id,
                    update_type='INFO_UPDATED',
                    changes_summary=json.dumps({'updated_fields': updated_fields}),
                    llm_summary=f"Updated: {pretty}.",
                )
```
(No row when `updated_fields` is empty.)

- [ ] **Step 4: Run, verify PASS**; then full suite `.venv/bin/python -m pytest -o addopts="" -q` + `manage.py check`.

- [ ] **Step 5: Commit**

```bash
git add apps/claims/views.py apps/claims/tests/test_refresh_timeline_delta.py
git commit -m "feat(claims): suppress no-op manual-refresh timeline rows; deterministic 'Updated: <fields>' body"
```

---

## Done criteria (Part B)
- A status-change timeline entry reads as a transition + what's-new delta (regression marked "(reopened)"), never the full re-narrated case; falls back to the transition line if the AI pass fails.
- A manual "refresh from Zendesk" that changes nothing writes NO timeline row; one that changes fields writes a deterministic "Updated: <fields>" row.
- `claim.ai_summary` (the snapshot shown on the claim) remains a full current-state summary.
- The summary prompt instructs the model to defer to facts over the status label's implied meaning.
- Full suite green; `manage.py check` clean.
