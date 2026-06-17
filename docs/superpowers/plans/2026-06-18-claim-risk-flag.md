# Claim Risk Flag (Part A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Project TDD note:** This repo follows strict, *blind* TDD — tests are authored from the behavioral spec **before** and **without sight of** the implementation, the RED failure is watched first, and if a test is found wrong during GREEN you abort back to RED rather than patch the test. The test code shown in each task is the **contract to author from**, not something to paste alongside the implementation. When executing with a subagent, give it the test intent; verify RED before writing the implementation.

**Goal:** Surface adversarial/risk signals on a claim (hostile client, refund demand, dispute risk, status regression) as a **sticky, acknowledgeable badge + filter** in management triage, so a negative signal is never erased by a later cheerful "Solved" summary.

**Architecture:** Risk is detected during the *existing* summary pass (no extra LLM call) — the briefing schema gains a risk read; a deterministic keyword booster and a deterministic status-regression check reinforce it. The signal is stored on `Claim` via a sticky `register_risk()` that only ever adds reasons / raises severity, cleared only by a human `acknowledge_risk()` (a new signal re-raises). Surfaced as a row badge + an "At risk (unacknowledged)" filter and a claim-detail banner with an Acknowledge button.

**Tech Stack:** Django 5.2, Django REST Framework (existing), Pydantic schemas (`apps/ai/schemas.py`), pytest. Run tests with `.venv/bin/python -m pytest <path> -o addopts="" -q` (the `python` binary is not on PATH).

**Reference spec:** `docs/superpowers/specs/2026-06-18-claim-risk-flag-and-delta-timeline-design.md`

---

## File structure

- **Modify** `apps/claims/models.py` — risk fields on `Claim`, constants (`RISK_LEVELS`, `RISK_RANK`, `RISK_REASON_CHOICES`), methods `register_risk()`, `acknowledge_risk()`, property `risk_active`.
- **Create** migration `apps/claims/migrations/00NN_claim_risk_fields.py` (generated).
- **Modify** `apps/ai/schemas.py` — add risk fields to `BriefingSummary` (with safe defaults).
- **Modify** `apps/integrations/briefing.py` — update `SUMMARY_PROMPT`; `generate_claim_summary` returns the validated object; add `keyword_risk_reasons()` + `merge_risk()`; `refresh_claim_summary` stores summary **and** registers risk.
- **Modify** `apps/integrations/views/webhooks.py` — deterministic status-regression in `mirror_status_change`.
- **Modify** `apps/users/views.py` — `claim_acknowledge_risk` view; `risk` filter in `agent_claims` + `manager_claims`.
- **Modify** `apps/users/urls.py` — acknowledge route.
- **Modify** `templates/agent/claims.html`, `templates/manager/claims.html` — risk filter control + row badge.
- **Modify** `templates/agent/claim_detail.html` — risk banner + Acknowledge form.
- **Create** tests: `apps/claims/tests/test_claim_risk_model.py`, `apps/integrations/tests/test_risk_detection.py`, `apps/users/tests/test_claim_risk_ui.py`.

---

## Task 1: Risk fields + sticky model logic on `Claim`

**Files:**
- Modify: `apps/claims/models.py` (constants near other module constants ~lines 8-24; fields in the workflow block after `status_changed_at` ~line 199; methods on the `Claim` class)
- Create: `apps/claims/tests/test_claim_risk_model.py`
- Migration: generated

- [ ] **Step 1: Write the failing tests** (`apps/claims/tests/test_claim_risk_model.py`)

```python
from django.contrib.auth import get_user_model
from django.test import TestCase
from apps.claims.models import Claim

User = get_user_model()


def _claim():
    return Claim.objects.create(client_email='c@example.com', zd_ticket_id='90001',
                                alf_claim_id='ALF9000001')


class RegisterRiskTests(TestCase):
    def test_first_signal_sets_level_reasons_detail_and_timestamps(self):
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='Client demanded refund')
        c.refresh_from_db()
        self.assertEqual(c.risk_level, 'at_risk')
        self.assertEqual(c.risk_reasons, ['refund_demanded'])
        self.assertEqual(c.risk_detail, 'Client demanded refund')
        self.assertIsNotNone(c.risk_first_flagged_at)
        self.assertIsNotNone(c.risk_last_signal_at)

    def test_clean_pass_never_downgrades(self):
        c = _claim()
        c.register_risk(reasons=['hostile_language'], level='at_risk', detail='scam')
        c.register_risk(reasons=[], level='none', detail='')  # later cheerful read
        c.refresh_from_db()
        self.assertEqual(c.risk_level, 'at_risk')
        self.assertEqual(c.risk_reasons, ['hostile_language'])

    def test_reasons_union_and_severity_only_rises(self):
        c = _claim()
        c.register_risk(reasons=['negative_sentiment'], level='watch', detail='unhappy')
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='wants money back')
        c.refresh_from_db()
        self.assertEqual(set(c.risk_reasons), {'negative_sentiment', 'refund_demanded'})
        self.assertEqual(c.risk_level, 'at_risk')

    def test_first_flagged_at_is_stable_across_signals(self):
        c = _claim()
        c.register_risk(reasons=['negative_sentiment'], level='watch', detail='x')
        first = Claim.objects.get(pk=c.pk).risk_first_flagged_at
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='y')
        self.assertEqual(Claim.objects.get(pk=c.pk).risk_first_flagged_at, first)


class AcknowledgeRiskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr', password='x')

    def test_acknowledge_clears_active_keeps_history(self):
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        c.acknowledge_risk(self.user)
        c.refresh_from_db()
        self.assertFalse(c.risk_active)                 # badge gone
        self.assertEqual(c.risk_level, 'at_risk')        # history kept
        self.assertEqual(c.risk_reasons, ['refund_demanded'])
        self.assertEqual(c.risk_acknowledged_by, self.user)

    def test_same_signal_after_ack_does_not_reraise(self):
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        c.acknowledge_risk(self.user)
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d again')
        c.refresh_from_db()
        self.assertFalse(c.risk_active)                 # still acknowledged

    def test_new_reason_after_ack_reraises(self):
        c = _claim()
        c.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        c.acknowledge_risk(self.user)
        c.register_risk(reasons=['status_regression'], level='at_risk', detail='reopened')
        c.refresh_from_db()
        self.assertTrue(c.risk_active)                  # re-raised
        self.assertIsNone(c.risk_acknowledged_at)
```

- [ ] **Step 2: Run the tests, verify they FAIL**

Run: `.venv/bin/python -m pytest apps/claims/tests/test_claim_risk_model.py -o addopts="" -q`
Expected: FAIL — `Claim` has no `register_risk` / risk fields (AttributeError / FieldError).

- [ ] **Step 3: Add constants + fields + methods** (`apps/claims/models.py`)

Near the other module constants (with `STATUS_FAMILIES`):
```python
RISK_LEVELS = [('none', 'None'), ('watch', 'Watch'), ('at_risk', 'At risk')]
RISK_RANK = {'none': 0, 'watch': 1, 'at_risk': 2}
_RANK_LEVEL = {0: 'none', 1: 'watch', 2: 'at_risk'}
RISK_REASON_CHOICES = [
    'hostile_language', 'refund_demanded', 'dispute_risk',
    'status_regression', 'negative_sentiment',
]
```

On the `Claim` model, after `status_changed_at` (keep existing fields byte-identical — see the migration-stability note at models.py:6-7):
```python
    # --- Client-risk flag (sticky; see docs/superpowers/specs/2026-06-18-...) ---
    risk_level = models.CharField(max_length=10, choices=RISK_LEVELS, default='none', blank=True)
    risk_reasons = models.JSONField(default=list, blank=True)   # list of RISK_REASON_CHOICES tags
    risk_detail = models.CharField(max_length=300, blank=True)
    risk_first_flagged_at = models.DateTimeField(null=True, blank=True)
    risk_last_signal_at = models.DateTimeField(null=True, blank=True)
    risk_acknowledged_at = models.DateTimeField(null=True, blank=True)
    risk_acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='acknowledged_claim_risks')
```
(`settings` is already imported for `AUTH_USER_MODEL` elsewhere in the model file; if not, add `from django.conf import settings`.)

Methods on `Claim`:
```python
    @property
    def risk_active(self) -> bool:
        """A raised risk that no one has acknowledged yet — what the badge/filter show."""
        return self.risk_level != 'none' and self.risk_acknowledged_at is None

    def register_risk(self, *, reasons, level, detail=''):
        """Sticky-merge a risk signal. Only ADDS reasons (union) and RAISES level —
        never downgrades, so a later clean read can't erase a flag. A genuinely new
        signal (a new reason, or the level rising) after an acknowledgement clears the
        acknowledgement so the badge returns. Saves only its own fields."""
        from django.utils import timezone
        reasons = [r for r in (reasons or []) if r]
        if level == 'none' and not reasons:
            return  # nothing to register; never downgrade
        existing = set(self.risk_reasons or [])
        incoming = set(reasons)
        old_rank = RISK_RANK.get(self.risk_level, 0)
        new_rank = max(old_rank, RISK_RANK.get(level, 0))
        is_new_signal = bool(incoming - existing) or new_rank > old_rank

        now = timezone.now()
        self.risk_reasons = sorted(existing | incoming)
        self.risk_level = _RANK_LEVEL[new_rank]
        if detail:
            self.risk_detail = detail[:300]
        if self.risk_first_flagged_at is None and new_rank > 0:
            self.risk_first_flagged_at = now
        self.risk_last_signal_at = now
        fields = ['risk_reasons', 'risk_level', 'risk_detail',
                  'risk_first_flagged_at', 'risk_last_signal_at', 'updated_at']
        if self.risk_acknowledged_at is not None and is_new_signal:
            self.risk_acknowledged_at = None
            self.risk_acknowledged_by = None
            fields += ['risk_acknowledged_at', 'risk_acknowledged_by']
        self.save(update_fields=fields)

    def acknowledge_risk(self, user):
        """Clear the active badge (records who/when). Keeps reasons/level for audit."""
        from django.utils import timezone
        self.risk_acknowledged_at = timezone.now()
        self.risk_acknowledged_by = user
        self.save(update_fields=['risk_acknowledged_at', 'risk_acknowledged_by', 'updated_at'])
```

- [ ] **Step 4: Generate the migration**

Run: `.venv/bin/python manage.py makemigrations claims`
Expected: a new migration adding the seven `risk_*` fields. Sanity check: `.venv/bin/python manage.py makemigrations --check --dry-run` reports nothing else changed.

- [ ] **Step 5: Run the tests, verify they PASS**

Run: `.venv/bin/python -m pytest apps/claims/tests/test_claim_risk_model.py -o addopts="" -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add apps/claims/models.py apps/claims/migrations/ apps/claims/tests/test_claim_risk_model.py
git commit -m "feat(claims): sticky risk fields + register_risk/acknowledge_risk on Claim"
```

---

## Task 2: Risk read in the summary pass (schema + keyword booster + wiring)

**Files:**
- Modify: `apps/ai/schemas.py` (`BriefingSummary`)
- Modify: `apps/integrations/briefing.py` (`SUMMARY_PROMPT`, `generate_claim_summary`, new `keyword_risk_reasons`, `merge_risk`, `refresh_claim_summary`)
- Create: `apps/integrations/tests/test_risk_detection.py`

- [ ] **Step 1: Write the failing tests** (`apps/integrations/tests/test_risk_detection.py`)

```python
from django.test import TestCase
from apps.integrations.briefing import keyword_risk_reasons, merge_risk


class KeywordBoosterTests(TestCase):
    def test_scam_flags_hostile(self):
        self.assertIn('hostile_language', keyword_risk_reasons('you people are a SCAM'))

    def test_chargeback_flags_dispute(self):
        self.assertIn('dispute_risk', keyword_risk_reasons('I will file a charge back'))

    def test_non_refundable_fee_is_not_flagged(self):
        # domain boilerplate appears on every claim — must NOT trip the booster
        self.assertEqual(keyword_risk_reasons('Client agreed to the non-refundable $76 fee'), set())

    def test_routine_dispute_word_not_flagged(self):
        self.assertEqual(keyword_risk_reasons('opened a PayPal dispute case earlier'), set())


class MergeRiskTests(TestCase):
    def test_ai_hard_reason_is_at_risk(self):
        level, reasons, _ = merge_risk(ai_level='at_risk', ai_reasons=['refund_demanded'],
                                       ai_note='wants money back', thread_text='')
        self.assertEqual(level, 'at_risk')
        self.assertIn('refund_demanded', reasons)

    def test_keyword_only_hard_reason_caps_at_watch(self):
        # AI saw nothing; only the keyword net fired -> watch, not at_risk (could be a quote)
        level, reasons, _ = merge_risk(ai_level='none', ai_reasons=[],
                                       ai_note='', thread_text='this is NOT a scam, just asking')
        self.assertEqual(level, 'watch')
        self.assertIn('hostile_language', reasons)

    def test_soft_sentiment_is_watch(self):
        level, _, _ = merge_risk(ai_level='watch', ai_reasons=['negative_sentiment'],
                                 ai_note='frustrated', thread_text='')
        self.assertEqual(level, 'watch')

    def test_clean_is_none(self):
        level, reasons, _ = merge_risk(ai_level='none', ai_reasons=[], ai_note='', thread_text='all good')
        self.assertEqual(level, 'none')
        self.assertEqual(reasons, [])
```

- [ ] **Step 2: Run, verify FAIL**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_risk_detection.py -o addopts="" -q`
Expected: FAIL — `keyword_risk_reasons` / `merge_risk` do not exist (ImportError).

- [ ] **Step 3: Extend the briefing schema** (`apps/ai/schemas.py`)

Add to `BriefingSummary` (defaults make it robust if the model omits the fields):
```python
    risk_level: Literal['none', 'watch', 'at_risk'] = 'none'
    risk_reasons: list[str] = Field(default_factory=list)
    risk_note: str = Field(default='', max_length=300)
```
(`Literal` and `Field` are already imported in this module — see `EmailCategorization`.)

- [ ] **Step 4: Add the booster + merge + wire into the summary pass** (`apps/integrations/briefing.py`)

Add near the top (after imports):
```python
import re

_RISK_KEYWORDS = [
    (re.compile(r'\bscam\b', re.I), 'hostile_language'),
    (re.compile(r'\bfrauds?\b', re.I), 'hostile_language'),
    (re.compile(r'charge\s?backs?', re.I), 'dispute_risk'),
    (re.compile(r'\b(lawyer|attorney)s?\b', re.I), 'dispute_risk'),
    (re.compile(r'\bBBB\b'), 'dispute_risk'),
]
# Deliberately excludes 'refund'/'dispute'/'complaint' — this business names a
# NON-REFUNDABLE fee on every claim and 'dispute' is routine PayPal vocabulary,
# so those words would flag nearly every case. Refund-demand detection is left to
# the AI (which can tell "demanded a refund" from "agreed to the non-refundable fee").
_HARD_REASONS = {'refund_demanded', 'dispute_risk', 'hostile_language', 'status_regression'}


def keyword_risk_reasons(text: str) -> set[str]:
    """Unambiguous escalation terms only. Returns the set of reason tags hit."""
    found = set()
    for rx, reason in _RISK_KEYWORDS:
        if rx.search(text or ''):
            found.add(reason)
    return found


def merge_risk(*, ai_level: str, ai_reasons, ai_note: str, thread_text: str):
    """Combine the AI risk read with the deterministic keyword booster.
    at_risk requires an AI-corroborated hard reason (or AI level at_risk);
    a keyword-only hard reason caps at 'watch' (it may be a quote, e.g. "not a scam")."""
    ai_reasons = set(ai_reasons or [])
    kw_reasons = keyword_risk_reasons(thread_text)
    reasons = sorted(ai_reasons | kw_reasons)
    if ai_level == 'at_risk' or (ai_reasons & _HARD_REASONS):
        level = 'at_risk'
    elif reasons:
        level = 'watch'
    else:
        level = 'none'
    return level, reasons, (ai_note or '').strip()
```

Update `SUMMARY_PROMPT` (briefing.py ~61-69) to request the risk read. Replace the `Respond as JSON: {"summary": "..."}` instruction with:
```python
SUMMARY_PROMPT = ALF_BUSINESS_CONTEXT + (
    "\n\nWrite a concise management summary of this claim's current state in `summary`.\n"
    "Also assess CLIENT risk (the paying customer, not the lost-and-found institutions):\n"
    "- risk_reasons: any of ['hostile_language','refund_demanded','dispute_risk',"
    "'negative_sentiment'] that the CLIENT exhibits. Use 'refund_demanded' only when the "
    "client asks for their money BACK — NOT when they merely agreed to the non-refundable fee. "
    "Use 'dispute_risk' for threats of a chargeback/PayPal dispute/legal action/BBB.\n"
    "- risk_level: 'at_risk' if any of those reasons is clearly present, else 'watch' for mild "
    "dissatisfaction, else 'none'.\n"
    "- risk_note: one short sentence naming the signal, or '' if none.\n"
    'Respond as JSON: {"summary": "...", "risk_level": "...", "risk_reasons": [...], "risk_note": "..."}.'
)
```

Change `generate_claim_summary` to return the validated object instead of just the string (it currently returns `result.summary.strip()`):
```python
def generate_claim_summary(claim, ticket_data):
    """Returns the validated BriefingSummary (summary + risk read), or None on AI failure."""
    # ... unchanged setup: build facts, untrusted thread, known_pii ...
    try:
        result = AIClient.complete(
            system_prompt=SUMMARY_PROMPT,
            trusted={'claim_facts': str(facts)},
            untrusted=untrusted,
            known_pii=known_pii,
            response_schema=BriefingSummary,
            call_site='claim_summary',
            temperature=SUMMARY_TEMPERATURE,
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        return result
    except Exception:
        logger.exception("Claim summary generation failed for claim %s", getattr(claim, 'id', '?'))
        return None
```

Add a thread-text helper for the keyword scan (joins what `build_ticket_thread` already assembles):
```python
def _thread_text(ticket_data) -> str:
    parts = [ticket_data.get('subject', ''), ticket_data.get('description', '')]
    parts += [c.get('body', '') for c in (ticket_data.get('comments') or [])]
    return '\n'.join(p for p in parts if p)
```

Update `refresh_claim_summary` to store the summary **and** register risk:
```python
def refresh_claim_summary(claim, ticket_data) -> bool:
    from django.utils import timezone
    result = generate_claim_summary(claim, ticket_data)
    if result is None:
        return False
    claim.ai_summary = result.summary.strip()
    claim.ai_summary_updated_at = timezone.now()
    claim.save(update_fields=['ai_summary', 'ai_summary_updated_at', 'updated_at'])
    level, reasons, note = merge_risk(
        ai_level=result.risk_level, ai_reasons=result.risk_reasons,
        ai_note=result.risk_note, thread_text=_thread_text(ticket_data))
    claim.register_risk(reasons=reasons, level=level, detail=note)
    return True
```

- [ ] **Step 5: Run, verify PASS**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_risk_detection.py -o addopts="" -q`
Expected: PASS.

- [ ] **Step 6: Run the briefing + summary regression tests to confirm no break**

Run: `.venv/bin/python -m pytest apps/integrations/ -o addopts="" -q`
Expected: PASS. (If a pre-existing test asserts `generate_claim_summary` returns a string, update it to read `.summary` — the return type intentionally changed; this is a legitimate spec change, fix the assertion at RED.)

- [ ] **Step 7: Commit**

```bash
git add apps/ai/schemas.py apps/integrations/briefing.py apps/integrations/tests/test_risk_detection.py
git commit -m "feat(briefing): risk read in the summary pass + keyword booster, wired into refresh_claim_summary"
```

---

## Task 3: Deterministic status-regression in the mirror

**Files:**
- Modify: `apps/integrations/views/webhooks.py` (`mirror_status_change`, ~lines 181-197)
- Test: add to `apps/integrations/tests/test_risk_detection.py`

- [ ] **Step 1: Write the failing test** (append to `test_risk_detection.py`)

```python
from unittest.mock import patch
from apps.claims.models import Claim
from apps.integrations.views import webhooks


def _solved_claim():
    return Claim.objects.create(client_email='r@example.com', zd_ticket_id='90100',
                                alf_claim_id='ALF9010000', status='Solved', status_category='solved')


class StatusRegressionTests(TestCase):
    @patch('apps.integrations.views.webhooks.refresh_claim_summary', return_value=True)
    @patch('apps.integrations.views.webhooks.fetch_zendesk_ticket', return_value={})
    @patch('apps.integrations.views.webhooks.fetch_zendesk_comments', return_value=[])
    @patch('apps.integrations.views.webhooks.resolve_custom_status',
           return_value={'name': 'Investigation initiated', 'category': 'open'})
    def test_solved_to_open_flags_regression(self, *_mocks):
        c = _solved_claim()
        webhooks.mirror_status_change(c, custom_status_id='123')
        c.refresh_from_db()
        self.assertIn('status_regression', c.risk_reasons)
        self.assertEqual(c.risk_level, 'at_risk')
        self.assertTrue(c.risk_active)

    @patch('apps.integrations.views.webhooks.refresh_claim_summary', return_value=True)
    @patch('apps.integrations.views.webhooks.fetch_zendesk_ticket', return_value={})
    @patch('apps.integrations.views.webhooks.fetch_zendesk_comments', return_value=[])
    @patch('apps.integrations.views.webhooks.resolve_custom_status',
           return_value={'name': 'Solved', 'category': 'solved'})
    def test_forward_to_solved_does_not_flag(self, *_mocks):
        c = Claim.objects.create(client_email='f@example.com', zd_ticket_id='90101',
                                 alf_claim_id='ALF9010100', status='Claim submitted', status_category='open')
        webhooks.mirror_status_change(c, custom_status_id='456')
        c.refresh_from_db()
        self.assertNotIn('status_regression', c.risk_reasons)
```

(Confirm the exact names imported into `webhooks.py` — `resolve_custom_status`, `fetch_zendesk_ticket`, `fetch_zendesk_comments`, `refresh_claim_summary` — and patch them where they are *used* in that module. Adjust patch targets if an import alias differs.)

- [ ] **Step 2: Run, verify FAIL**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_risk_detection.py::StatusRegressionTests -o addopts="" -q`
Expected: FAIL — no `status_regression` reason is registered.

- [ ] **Step 3: Add the regression check** (`apps/integrations/views/webhooks.py`)

Inside `mirror_status_change`, after `old_status = claim.status` is captured and the new `resolved` is known, capture the old category and detect a terminal→non-terminal reopen. Add **after** the `transaction.atomic()` block (so it runs outside the status-write transaction, consistent with the existing AI back-fill convention):
```python
        # Deterministic status regression: a terminal (Solved) claim reopened to a
        # non-terminal stage is a red flag (e.g. an agent bouncing a refund dispute
        # back to 'Investigation initiated'). Only this unambiguous case is hard-flagged.
        if old_category == 'solved' and resolved['category'] != 'solved':
            claim.register_risk(
                reasons=['status_regression'], level='at_risk',
                detail=f"Reopened from Solved to '{new_status}'")
```
Capture `old_category = claim.status_category` next to `old_status = claim.status` (before the save overwrites it). `register_risk` saves its own fields, so no change to the mirror's `update_fields` list is needed.

- [ ] **Step 4: Run, verify PASS**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_risk_detection.py -o addopts="" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/integrations/views/webhooks.py apps/integrations/tests/test_risk_detection.py
git commit -m "feat(webhooks): flag Solved->reopened status regression as a claim risk"
```

---

## Task 4: Acknowledge action (view + URL)

**Files:**
- Modify: `apps/users/views.py` (new `claim_acknowledge_risk`, near the other claim-detail POST actions ~lines 306-397)
- Modify: `apps/users/urls.py` (~lines 19-24)
- Create: `apps/users/tests/test_claim_risk_ui.py`

- [ ] **Step 1: Write the failing test** (`apps/users/tests/test_claim_risk_ui.py`)

```python
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from apps.claims.models import Claim

User = get_user_model()


class AcknowledgeRiskViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr', password='x')
        self.client.force_login(self.user)
        self.claim = Claim.objects.create(client_email='c@example.com', zd_ticket_id='90200',
                                          alf_claim_id='ALF9020000')
        self.claim.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')

    def test_post_acknowledges(self):
        resp = self.client.post(reverse('claim_acknowledge_risk', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 302)  # redirect back to detail
        self.claim.refresh_from_db()
        self.assertFalse(self.claim.risk_active)
        self.assertEqual(self.claim.risk_acknowledged_by, self.user)

    def test_get_not_allowed(self):
        resp = self.client.get(reverse('claim_acknowledge_risk', args=[self.claim.id]))
        self.assertIn(resp.status_code, (405, 302))  # POST-only (or redirected, not acknowledged)
        self.claim.refresh_from_db()
        self.assertTrue(self.claim.risk_active)

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.post(reverse('claim_acknowledge_risk', args=[self.claim.id]))
        self.assertEqual(resp.status_code, 302)  # to login
        self.claim.refresh_from_db()
        self.assertTrue(self.claim.risk_active)
```

- [ ] **Step 2: Run, verify FAIL**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_risk_ui.py::AcknowledgeRiskViewTests -o addopts="" -q`
Expected: FAIL — `NoReverseMatch` (route doesn't exist).

- [ ] **Step 3: Add the view** (`apps/users/views.py`, follow the Pattern-B function-view style of `client_updates_start` etc.)

```python
@agent_required
def claim_acknowledge_risk(request, claim_id):
    """Acknowledge a claim's risk flag (clears the active badge). POST-only."""
    claim = get_object_or_404(Claim, id=claim_id)
    if request.method != 'POST':
        return redirect('agent_claim_detail', claim_id=claim_id)
    if claim.risk_active:
        claim.acknowledge_risk(request.user)
        messages.success(request, 'Risk flag acknowledged.')
    return redirect('agent_claim_detail', claim_id=claim_id)
```
(`agent_required`, `get_object_or_404`, `messages`, `redirect`, `Claim` are already imported in this module — confirm and add any that aren't.)

- [ ] **Step 4: Add the URL** (`apps/users/urls.py`, alongside the other `agent/claims/<int:claim_id>/...` routes)

```python
    path('agent/claims/<int:claim_id>/acknowledge-risk/', views.claim_acknowledge_risk,
         name='claim_acknowledge_risk'),
```

- [ ] **Step 5: Run, verify PASS**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_risk_ui.py::AcknowledgeRiskViewTests -o addopts="" -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/users/views.py apps/users/urls.py apps/users/tests/test_claim_risk_ui.py
git commit -m "feat(users): acknowledge-risk claim action (POST, login-gated)"
```

---

## Task 5: List badge + "At risk" filter

**Files:**
- Modify: `apps/users/views.py` (`agent_claims` ~212-256, `manager_claims` ~754-844)
- Modify: `templates/agent/claims.html`, `templates/manager/claims.html`
- Test: add to `apps/users/tests/test_claim_risk_ui.py`

- [ ] **Step 1: Write the failing test** (append to `test_claim_risk_ui.py`)

```python
class RiskFilterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr2', password='x')
        self.client.force_login(self.user)
        self.flagged = Claim.objects.create(client_email='a@example.com', zd_ticket_id='90300',
                                             alf_claim_id='ALF9030000', client_name='Risky Rita')
        self.flagged.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        self.clean = Claim.objects.create(client_email='b@example.com', zd_ticket_id='90301',
                                          alf_claim_id='ALF9030100', client_name='Calm Carl')

    def test_risk_filter_shows_only_unacknowledged_flagged(self):
        resp = self.client.get(reverse('agent_claims') + '?risk=1')
        self.assertContains(resp, 'ALF9030000')
        self.assertNotContains(resp, 'ALF9030100')

    def test_unfiltered_shows_both(self):
        resp = self.client.get(reverse('agent_claims'))
        self.assertContains(resp, 'ALF9030000')
        self.assertContains(resp, 'ALF9030100')

    def test_acknowledged_claim_drops_out_of_risk_filter(self):
        self.flagged.acknowledge_risk(self.user)
        resp = self.client.get(reverse('agent_claims') + '?risk=1')
        self.assertNotContains(resp, 'ALF9030000')

    def test_badge_rendered_on_list(self):
        resp = self.client.get(reverse('agent_claims'))
        self.assertContains(resp, 'At risk')  # the badge label
```

(Confirm the reverse name for the agent claim list — recon shows `agent_claims` renders `agent/claims.html`; verify the URL name in `apps/users/urls.py`.)

- [ ] **Step 2: Run, verify FAIL**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_risk_ui.py::RiskFilterTests -o addopts="" -q`
Expected: FAIL — no `risk` filter / no badge.

- [ ] **Step 3: Add the filter to the views** (`agent_claims` and `manager_claims`, mirroring the existing `status_filter` pattern)

```python
    risk_filter = request.GET.get('risk')
    if risk_filter:
        claims = claims.filter(risk_acknowledged_at__isnull=True).exclude(risk_level='none')
    # ... existing pagination ...
    context['risk_filter'] = risk_filter   # add to the existing context dict
```

- [ ] **Step 4: Add the badge + filter control to the templates**

In `templates/agent/claims.html` and `templates/manager/claims.html`:

Filter control (inside the existing `<form method="get">` filter card, next to the status `<select>`):
```html
<label class="label cursor-pointer gap-2">
  <span class="label-text">At risk only</span>
  <input type="checkbox" name="risk" value="1" class="checkbox checkbox-error"
         {% if risk_filter %}checked{% endif %} onchange="this.form.submit()">
</label>
```

Row badge (in the claim row, near the status pill — `claim.risk_active` drives it):
```html
{% if claim.risk_active %}
  <span class="badge badge-error gap-1" title="{{ claim.risk_reasons|join:', ' }} — {{ claim.risk_detail }}">
    ⚠ At risk
  </span>
{% endif %}
```

Pagination links: append `&risk={{ risk_filter }}` wherever the existing links append `&status={{ status_filter }}` (so the filter survives paging).

- [ ] **Step 5: Run, verify PASS**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_risk_ui.py::RiskFilterTests -o addopts="" -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/users/views.py templates/agent/claims.html templates/manager/claims.html apps/users/tests/test_claim_risk_ui.py
git commit -m "feat(claims-list): At-risk badge + unacknowledged-risk filter"
```

---

## Task 6: Claim-detail risk banner + Acknowledge button

**Files:**
- Modify: `templates/agent/claim_detail.html` (banner after the header `</div>` ~line 48, before the "Client updates" card ~line 50)
- Test: add to `apps/users/tests/test_claim_risk_ui.py`

- [ ] **Step 1: Write the failing test** (append to `test_claim_risk_ui.py`)

```python
class RiskBannerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='mgr3', password='x')
        self.client.force_login(self.user)
        self.claim = Claim.objects.create(client_email='c@example.com', zd_ticket_id='90400',
                                          alf_claim_id='ALF9040000', client_name='Test')

    def test_banner_shown_when_at_risk(self):
        self.claim.register_risk(reasons=['refund_demanded'], level='at_risk',
                                 detail='Client demanded a refund')
        resp = self.client.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertContains(resp, 'Client demanded a refund')
        self.assertContains(resp, 'Acknowledge')

    def test_no_banner_when_clean(self):
        resp = self.client.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertNotContains(resp, 'Acknowledge')

    def test_no_banner_after_acknowledge(self):
        self.claim.register_risk(reasons=['refund_demanded'], level='at_risk', detail='d')
        self.claim.acknowledge_risk(self.user)
        resp = self.client.get(reverse('agent_claim_detail', args=[self.claim.id]))
        self.assertNotContains(resp, 'Acknowledge')
```

- [ ] **Step 2: Run, verify FAIL**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_risk_ui.py::RiskBannerTests -o addopts="" -q`
Expected: FAIL — banner not present.

- [ ] **Step 3: Add the banner** (`templates/agent/claim_detail.html`, immediately after the page-header block, before the "Client updates" card)

```html
{% if claim.risk_active %}
<div class="card-modern p-6 mb-6 animate-fade-in border border-error/40 bg-error/5">
  <div class="flex items-start justify-between gap-4">
    <div>
      <h3 class="font-semibold text-error flex items-center gap-2">⚠ Client risk flag</h3>
      <p class="mt-1 text-sm">{{ claim.risk_detail }}</p>
      <p class="mt-1 text-xs opacity-70">
        Reasons: {{ claim.risk_reasons|join:", " }}
        · first flagged {{ claim.risk_first_flagged_at|date:"M j, Y H:i" }}
      </p>
    </div>
    <form method="post" action="{% url 'claim_acknowledge_risk' claim.id %}">
      {% csrf_token %}
      <button type="submit" class="btn btn-sm btn-error">Acknowledge</button>
    </form>
  </div>
</div>
{% endif %}
```

- [ ] **Step 4: Run, verify PASS**

Run: `.venv/bin/python -m pytest apps/users/tests/test_claim_risk_ui.py -o addopts="" -q`
Expected: PASS (all UI tests).

- [ ] **Step 5: Full suite + system check**

Run: `.venv/bin/python -m pytest -o addopts="" -q` then `.venv/bin/python manage.py check`
Expected: all pass; check clean.

- [ ] **Step 6: Commit**

```bash
git add templates/agent/claim_detail.html apps/users/tests/test_claim_risk_ui.py
git commit -m "feat(claim-detail): client-risk banner with Acknowledge button"
```

---

## Done criteria (Part A)

- A claim where the client is hostile / demands a refund / threatens a dispute, or whose status regressed from Solved, shows a sticky **⚠ At risk** badge in the list and a banner on the detail page.
- A later clean summary does **not** clear it; only a manager's **Acknowledge** does (recorded); a new signal afterward re-raises it.
- An **"At risk only"** filter surfaces exactly the unacknowledged-flagged claims.
- Full suite green; `manage.py check` clean.

Part B (delta-aware timeline + boilerplate-contradiction fix) is a separate plan.
