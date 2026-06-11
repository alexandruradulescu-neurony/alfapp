# Claim Status Mirror + Real Summary Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claims mirror their Zendesk ticket's custom status (name + family) automatically; the claim's stored AI summary becomes a real `AIClient` product regenerated at lifecycle points; LORA stops writing stages anywhere; deadlines become computable; dashboards count by status family.

**Architecture:** Zendesk webhooks (already received) become the only stage writer. A cached resolver translates Zendesk custom-status ids → (agent_label, status_category). A new `apps/integrations/briefing.py` hosts the shared business context + the summary engine (`generate_claim_summary`/`refresh_claim_summary`) used by the webhook (creation + status change) and the rebuilt "Refresh from Zendesk" view. All AI calls go through `apps/ai/AIClient` (PII tokenization, never a passthrough).

**Tech Stack:** Django 5.2, DRF, pytest (`.venv/bin/python -m pytest`), zoneinfo (stdlib), Django LocMem cache.

**Spec:** `docs/superpowers/specs/2026-06-11-claim-entity-redesign-design.md` — read it first.

**Conventions for every task:** run tests with `.venv/bin/python -m pytest <path> -q`; commit after each green task with the message given; NEVER commit the pre-existing modified `.gitignore` (leave it untouched); work directly on `main` (user-authorized).

---

### Task 1: Fix `ClaimUpdateTimeline.__str__`

**Files:**
- Modify: `apps/claims/models.py:260`
- Test: `apps/claims/tests/test_claim_model.py`

- [ ] **Step 1: Write the failing test** (append to `apps/claims/tests/test_claim_model.py`)

```python
class ClaimUpdateTimelineStrTests(TestCase):
    def test_str_does_not_crash_and_mentions_update_type(self):
        from apps.claims.models import Claim, ClaimUpdateTimeline
        claim = Claim.objects.create(client_email='str-test@example.com')
        entry = ClaimUpdateTimeline.objects.create(
            claim=claim, zendesk_ticket_id='123', update_type='STATUS_CHANGE',
        )
        text = str(entry)
        self.assertIn('STATUS_CHANGE', text)
        self.assertIn(str(claim.id), text)
```

Ensure the file imports `TestCase` from `django.test` (it already does).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest apps/claims/tests/test_claim_model.py -q -k str_does_not_crash`
Expected: FAIL with `NameError: name 'date' is not defined`

- [ ] **Step 3: Fix the implementation** — in `apps/claims/models.py:260` replace:

```python
        return f"Update for Claim #{self.claim_id} - {self.update_type} ({self.created_at|date:'M d, Y'})"
```

with:

```python
        return f"Update for Claim #{self.claim_id} - {self.update_type} ({self.created_at:%b %d, %Y})"
```

- [ ] **Step 4: Run test to verify it passes** — same command, expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/claims/models.py apps/claims/tests/test_claim_model.py
git commit -m "fix(claims): ClaimUpdateTimeline.__str__ used template syntax in an f-string"
```

---

### Task 2: Claim model — status mirror fields

**Files:**
- Modify: `apps/claims/models.py` (Claim model)
- Modify: `apps/integrations/services.py:1006` (`get_status_display` caller)
- Test: `apps/claims/tests/test_claim_model.py`
- Create: migration via `makemigrations`

- [ ] **Step 1: Write failing tests** (append to `apps/claims/tests/test_claim_model.py`)

```python
class ClaimStatusMirrorFieldTests(TestCase):
    def test_new_claim_defaults_to_investigation_initiated_open_family(self):
        from apps.claims.models import Claim
        claim = Claim.objects.create(client_email='mirror@example.com')
        self.assertEqual(claim.status, 'Investigation initiated')
        self.assertEqual(claim.status_category, 'open')
        self.assertIsNone(claim.status_changed_at)
        self.assertIsNone(claim.deadline_at)
        self.assertIsNone(claim.ai_summary_updated_at)

    def test_status_accepts_long_zendesk_names(self):
        from apps.claims.models import Claim
        claim = Claim.objects.create(
            client_email='long@example.com',
            status='Closed - Client Not Answering', status_category='solved',
        )
        claim.refresh_from_db()
        self.assertEqual(claim.status, 'Closed - Client Not Answering')
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest apps/claims/tests/test_claim_model.py -q -k MirrorField`
Expected: FAIL (`status` default is `'Received'`; new fields missing)

- [ ] **Step 3: Edit `apps/claims/models.py`** — replace the `STATUS_CHOICES` block (lines 10-19) and the `status` field (lines 145-151), and add the new fields:

Delete the `STATUS_CHOICES = [...]` list entirely. Add in its place:

```python
    # Zendesk custom-status families (status_category in the Zendesk API).
    STATUS_FAMILIES = [
        ('new', 'New'),
        ('open', 'Open'),
        ('pending', 'Pending'),
        ('hold', 'On hold'),
        ('solved', 'Solved'),
    ]
```

Replace the `status` field definition with:

```python
    status = models.CharField(
        max_length=64,
        default='Investigation initiated',
        help_text='Zendesk custom status name (agent view), mirrored verbatim from the ticket'
    )
    status_category = models.CharField(
        max_length=10,
        choices=STATUS_FAMILIES,
        default='open',
        blank=True,
        help_text="Zendesk status family — drives grouping/colors; '' when unknown"
    )
    status_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the current status was set (from the Zendesk webhook)'
    )
```

After the `deadline_timezone` field add:

```python
    deadline_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Computed deadline moment (date + best-effort time/timezone); urgency math uses this'
    )
```

After `ai_summary` add:

```python
    ai_summary_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='When the AI summary was last regenerated'
    )
```

In `class Meta.indexes` add:

```python
            models.Index(fields=['status_category', '-created_at']),
```

- [ ] **Step 4: Fix the `get_status_display` caller** — without `choices`, Django no longer generates it. In `apps/integrations/services.py:1006` replace:

```python
        'status': claim.get_status_display(),
```

with:

```python
        'status': claim.status,
```

Then verify it was the only caller: `grep -rn "get_status_display" apps/ templates/ --include="*.py" --include="*.html" | grep -v refund | grep -v doc_type` — expect no remaining Claim hits (Refund/DisputeDocument keep their own choices).

- [ ] **Step 5: Make and apply the migration**

```bash
.venv/bin/python manage.py makemigrations claims
.venv/bin/python manage.py migrate
```

Expected: one new migration with `RemoveField`-free output (alter `status`, add 3 fields, add index).

- [ ] **Step 6: Run the model tests**

Run: `.venv/bin/python -m pytest apps/claims/tests/test_claim_model.py -q`
Expected: the two new tests PASS; if pre-existing tests in this file assert `status='Received'` defaults or `STATUS_CHOICES`, update those assertions to the new default/vocabulary now.

- [ ] **Step 7: Commit**

```bash
git add apps/claims/models.py apps/claims/migrations/ apps/integrations/services.py apps/claims/tests/test_claim_model.py
git commit -m "feat(claims): status mirrors Zendesk names + family, status_changed_at, deadline_at, ai_summary_updated_at"
```

---

### Task 3: Deadline parser (`compute_deadline_at`)

**Files:**
- Create: `apps/claims/services.py`
- Create: `apps/claims/tests/test_claim_services.py`

- [ ] **Step 1: Write failing tests** — create `apps/claims/tests/test_claim_services.py`:

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.test import TestCase

from apps.claims.services import compute_deadline_at


class ComputeDeadlineAtTests(TestCase):
    def test_no_date_returns_none(self):
        self.assertIsNone(compute_deadline_at(None, '17:00', 'CET'))

    def test_date_only_defaults_to_end_of_day_utc(self):
        result = compute_deadline_at(date(2026, 7, 1), '', '')
        self.assertEqual(result, datetime(2026, 7, 1, 23, 59, 59, tzinfo=ZoneInfo('UTC')))

    def test_24h_time_and_iana_timezone(self):
        result = compute_deadline_at(date(2026, 7, 1), '17:00', 'Europe/Paris')
        self.assertEqual(result, datetime(2026, 7, 1, 17, 0, tzinfo=ZoneInfo('Europe/Paris')))

    def test_12h_time_and_abbreviation(self):
        result = compute_deadline_at(date(2026, 7, 1), '5 PM', 'CET')
        self.assertEqual(result.hour, 17)
        self.assertEqual(str(result.tzinfo), 'Europe/Paris')

    def test_dotted_time(self):
        result = compute_deadline_at(date(2026, 7, 1), '17.30', 'UTC')
        self.assertEqual((result.hour, result.minute), (17, 30))

    def test_garbage_time_and_timezone_fall_back(self):
        result = compute_deadline_at(date(2026, 7, 1), 'soonish', 'Mars/Phobos')
        self.assertEqual((result.hour, result.minute, result.second), (23, 59, 59))
        self.assertEqual(str(result.tzinfo), 'UTC')

    def test_12am_is_midnight(self):
        result = compute_deadline_at(date(2026, 7, 1), '12 AM', 'UTC')
        self.assertEqual(result.hour, 0)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest apps/claims/tests/test_claim_services.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.claims.services'`

- [ ] **Step 3: Implement** — create `apps/claims/services.py`:

```python
"""Claim-domain pure helpers (no model imports — safe for migrations)."""
import re
from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Common human-typed abbreviations -> IANA zone. Fallback is UTC; precision
# beyond "right day" is best-effort by design (see spec §6).
TZ_ABBREVIATIONS = {
    'UTC': 'UTC', 'GMT': 'UTC', 'Z': 'UTC',
    'CET': 'Europe/Paris', 'CEST': 'Europe/Paris',
    'EET': 'Europe/Bucharest', 'EEST': 'Europe/Bucharest',
    'BST': 'Europe/London', 'WET': 'Europe/Lisbon',
    'EST': 'America/New_York', 'EDT': 'America/New_York',
    'CST': 'America/Chicago', 'CDT': 'America/Chicago',
    'MST': 'America/Denver', 'MDT': 'America/Denver',
    'PST': 'America/Los_Angeles', 'PDT': 'America/Los_Angeles',
}

_TIME_PATTERN = re.compile(r'^\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\s*$', re.IGNORECASE)

_END_OF_DAY = time(23, 59, 59)


def parse_deadline_time(text: str) -> Optional[time]:
    """'17:00', '17.30', '5 PM', '5:30pm' -> time; anything else -> None."""
    match = _TIME_PATTERN.match(text or '')
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or '').lower()
    if meridiem == 'pm' and hour != 12:
        hour += 12
    elif meridiem == 'am' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def parse_deadline_timezone(text: str) -> ZoneInfo:
    """IANA name or known abbreviation -> ZoneInfo; anything else -> UTC."""
    cleaned = (text or '').strip()
    if cleaned:
        try:
            return ZoneInfo(cleaned)
        except (ZoneInfoNotFoundError, ValueError):
            mapped = TZ_ABBREVIATIONS.get(cleaned.upper())
            if mapped:
                return ZoneInfo(mapped)
    return ZoneInfo('UTC')


def compute_deadline_at(deadline_date: Optional[date],
                        deadline_time: str = '',
                        deadline_timezone: str = '') -> Optional[datetime]:
    """Best-effort deadline moment. No date -> None. Unparseable time ->
    end of day; unparseable timezone -> UTC."""
    if not deadline_date:
        return None
    moment = parse_deadline_time(deadline_time) or _END_OF_DAY
    tz = parse_deadline_timezone(deadline_timezone)
    return datetime.combine(deadline_date, moment, tzinfo=tz)
```

- [ ] **Step 4: Run tests** — same command, expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/claims/services.py apps/claims/tests/test_claim_services.py
git commit -m "feat(claims): compute_deadline_at — best-effort deadline moment from date + free-text time/tz"
```

---

### Task 4: Legacy status remap + data migration

**Files:**
- Create: `apps/claims/legacy_status_map.py`
- Create: data migration in `apps/claims/migrations/`
- Test: `apps/claims/tests/test_claim_services.py` (append)

- [ ] **Step 1: Write failing tests** (append to `apps/claims/tests/test_claim_services.py`)

```python
class LegacyStatusMapTests(TestCase):
    def test_all_legacy_values_map(self):
        from apps.claims.legacy_status_map import map_legacy_status
        expected = {
            'Received': ('Investigation initiated', 'open'),
            'Searching': ('Claim submitted', 'open'),
            'Found': ('Object Found', 'open'),
            'Shipped': ('Object Found', 'open'),
            'Disputed': ('Open', 'open'),
            'REFUND_REQUESTED': ('Refund Requested', 'open'),
            'REFUNDED': ('Closed - Refunded', 'solved'),
            'PARTIALLY_REFUNDED': ('Closed - Refunded', 'solved'),
        }
        for old, new in expected.items():
            self.assertEqual(map_legacy_status(old), new)

    def test_unknown_value_passes_through_with_open_family(self):
        from apps.claims.legacy_status_map import map_legacy_status
        self.assertEqual(map_legacy_status('Investigation initiated'),
                         ('Investigation initiated', 'open'))
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError`

- [ ] **Step 3: Implement** — create `apps/claims/legacy_status_map.py`:

```python
"""One-shot mapping of pre-mirror LORA statuses to Zendesk status names.
Used by the data migration; kept importable so it stays unit-tested."""

LEGACY_STATUS_MAP = {
    'Received': ('Investigation initiated', 'open'),
    'Searching': ('Claim submitted', 'open'),
    'Found': ('Object Found', 'open'),
    'Shipped': ('Object Found', 'open'),
    'Disputed': ('Open', 'open'),
    'REFUND_REQUESTED': ('Refund Requested', 'open'),
    'REFUNDED': ('Closed - Refunded', 'solved'),
    'PARTIALLY_REFUNDED': ('Closed - Refunded', 'solved'),
}


def map_legacy_status(old: str) -> tuple[str, str]:
    return LEGACY_STATUS_MAP.get(old, (old, 'open'))
```

- [ ] **Step 4: Create the data migration**

```bash
.venv/bin/python manage.py makemigrations claims --empty -n remap_legacy_statuses
```

Fill the generated file with (keep the auto-generated `dependencies`):

```python
from django.db import migrations


def remap_statuses(apps, schema_editor):
    from apps.claims.legacy_status_map import map_legacy_status
    from apps.claims.services import compute_deadline_at

    Claim = apps.get_model('claims', 'Claim')
    for claim in Claim.objects.all().iterator():
        name, family = map_legacy_status(claim.status)
        claim.status = name
        claim.status_category = family
        claim.status_changed_at = claim.updated_at
        claim.deadline_at = compute_deadline_at(
            claim.deadline_date, claim.deadline_time, claim.deadline_timezone)
        claim.save(update_fields=[
            'status', 'status_category', 'status_changed_at', 'deadline_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('claims', 'XXXX_previous_auto_name'),  # keep what makemigrations generated
    ]

    operations = [
        migrations.RunPython(remap_statuses, migrations.RunPython.noop),
    ]
```

- [ ] **Step 5: Apply + run tests**

```bash
.venv/bin/python manage.py migrate
.venv/bin/python -m pytest apps/claims/tests/test_claim_services.py -q
```

Expected: migrate OK; all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/claims/legacy_status_map.py apps/claims/migrations/ apps/claims/tests/test_claim_services.py
git commit -m "feat(claims): data migration remapping legacy statuses to Zendesk vocabulary"
```

---

### Task 5: Custom-status resolver with cache

**Files:**
- Modify: `apps/integrations/services.py` (add below `fetch_zendesk_user`, ~line 338)
- Test: `apps/integrations/tests/test_zendesk_services.py` (append)

- [ ] **Step 1: Write failing tests** (append to `apps/integrations/tests/test_zendesk_services.py`; follow the file's existing SystemSettings setup pattern — it already creates one for fetch tests):

```python
from unittest.mock import patch

from django.core.cache import cache


class ResolveCustomStatusTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch('apps.integrations.services._fetch_custom_statuses')
    def test_resolves_known_id_and_caches(self, mock_fetch):
        from apps.integrations.services import resolve_custom_status
        mock_fetch.return_value = {
            '111': {'name': 'Claim submitted', 'category': 'open'},
        }
        result = resolve_custom_status('111')
        self.assertEqual(result, {'name': 'Claim submitted', 'category': 'open'})
        resolve_custom_status('111')  # second call served from cache
        self.assertEqual(mock_fetch.call_count, 1)

    @patch('apps.integrations.services._fetch_custom_statuses')
    def test_unknown_id_refreshes_then_falls_back(self, mock_fetch):
        from apps.integrations.services import resolve_custom_status
        mock_fetch.return_value = {'111': {'name': 'Open', 'category': 'open'}}
        result = resolve_custom_status('999')
        self.assertEqual(result, {'name': '999', 'category': ''})
        self.assertEqual(mock_fetch.call_count, 1)

    @patch('apps.integrations.services._fetch_custom_statuses', side_effect=ValueError('no creds'))
    def test_fetch_failure_falls_back_to_id(self, mock_fetch):
        from apps.integrations.services import resolve_custom_status
        result = resolve_custom_status('123')
        self.assertEqual(result, {'name': '123', 'category': ''})
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_zendesk_services.py -q -k ResolveCustomStatus`
Expected: FAIL with `ImportError` / `AttributeError` (function missing)

- [ ] **Step 3: Implement** in `apps/integrations/services.py` (after `fetch_zendesk_user`). Add `from django.core.cache import cache` to the module imports.

```python
CUSTOM_STATUS_CACHE_KEY = 'zd_custom_statuses_v1'
CUSTOM_STATUS_CACHE_TTL = 60 * 60 * 24  # 24h; unknown ids force a refresh anyway


def _fetch_custom_statuses() -> Dict[str, Dict[str, str]]:
    """GET /api/v2/custom_statuses.json -> {id: {'name', 'category'}}.
    Raises on configuration/network errors (caller decides the fallback)."""
    base_url = _get_zendesk_base_url()
    headers = _get_zendesk_auth_headers()
    req = urllib.request.Request(f"{base_url}/custom_statuses.json", headers=headers, method='GET')
    timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = json.loads(response.read().decode('utf-8'))
    mapping = {}
    for cs in result.get('custom_statuses', []):
        mapping[str(cs.get('id'))] = {
            'name': cs.get('agent_label', '') or '',
            'category': cs.get('status_category', '') or '',
        }
    logger.info(f"Fetched {len(mapping)} Zendesk custom statuses")
    return mapping


def resolve_custom_status(status_id) -> Dict[str, str]:
    """Translate a Zendesk custom-status id to {'name', 'category'}.
    Cached; an unknown id forces one refresh (covers statuses added in
    Zendesk after the cache was filled). Total failure -> id as name,
    empty category — the webhook still mirrors *something* traceable."""
    sid = str(status_id)
    mapping = cache.get(CUSTOM_STATUS_CACHE_KEY)
    if mapping is None or sid not in mapping:
        try:
            mapping = _fetch_custom_statuses()
            cache.set(CUSTOM_STATUS_CACHE_KEY, mapping, CUSTOM_STATUS_CACHE_TTL)
        except Exception as e:
            logger.error(f"Could not fetch Zendesk custom statuses: {e}")
            mapping = mapping or {}
    entry = mapping.get(sid)
    if not entry:
        logger.warning(f"Unknown Zendesk custom status id {sid}; mirroring id verbatim")
        return {'name': sid, 'category': ''}
    return entry
```

- [ ] **Step 4: Run tests** — same command, expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/integrations/services.py apps/integrations/tests/test_zendesk_services.py
git commit -m "feat(integrations): cached Zendesk custom-status resolver (id -> name + family)"
```

---

### Task 6: `apps/integrations/briefing.py` — shared context + summary engine

**Files:**
- Create: `apps/integrations/briefing.py`
- Modify: `apps/integrations/views.py` (lines 339-354: move `ALF_BUSINESS_CONTEXT` out, import instead)
- Test: create `apps/integrations/tests/test_briefing.py`

- [ ] **Step 1: Write failing tests** — create `apps/integrations/tests/test_briefing.py`:

```python
from unittest.mock import patch

from django.test import TestCase

from apps.claims.models import Claim


def _fake_briefing(summary='AI summary of the case.'):
    from apps.ai.schemas import BriefingSummary
    return BriefingSummary(summary=summary, next_steps=[])


class NormalizeFetchedCommentsTests(TestCase):
    def test_author_dict_and_body_are_flattened(self):
        from apps.integrations.briefing import normalize_fetched_comments
        raw = [{'author': {'id': 1, 'name': 'TSA Office', 'email': 't@x.gov'},
                'body': 'No match found yet.', 'public': False,
                'created_at': '2026-06-01T10:00:00Z'}]
        result = normalize_fetched_comments(raw)
        self.assertEqual(result, [{'author': 'TSA Office',
                                   'created_at': '2026-06-01T10:00:00Z',
                                   'public': False,
                                   'text': 'No match found yet.'}])

    def test_non_dict_entries_are_skipped(self):
        from apps.integrations.briefing import normalize_fetched_comments
        self.assertEqual(normalize_fetched_comments(['plain', None]), [])


class GenerateClaimSummaryTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(
            client_email='sum@example.com', client_name='Ana Pop',
            zd_ticket_id='777', object_description='Black wallet')
        self.ticket_data = {'subject': 'ALF1234567', 'description': 'Lost wallet',
                            'created_at': '2026-06-01T09:00:00Z', 'comments': []}

    @patch('apps.integrations.briefing.AIClient.complete')
    def test_returns_summary_and_passes_client_name_as_known_pii(self, mock_complete):
        from apps.integrations.briefing import generate_claim_summary
        mock_complete.return_value = _fake_briefing('Case is searching.')
        result = generate_claim_summary(self.claim, self.ticket_data)
        self.assertEqual(result, 'Case is searching.')
        kwargs = mock_complete.call_args.kwargs
        self.assertIn('Ana Pop', kwargs['known_pii']['names'])
        self.assertEqual(kwargs['call_site'], 'claim_summary')

    @patch('apps.integrations.briefing.AIClient.complete', side_effect=RuntimeError('AI down'))
    def test_ai_failure_returns_none(self, mock_complete):
        from apps.integrations.briefing import generate_claim_summary
        self.assertIsNone(generate_claim_summary(self.claim, self.ticket_data))


class RefreshClaimSummaryTests(TestCase):
    def setUp(self):
        self.claim = Claim.objects.create(
            client_email='ref@example.com', zd_ticket_id='778',
            ai_summary='old text')
        self.ticket_data = {'subject': 's', 'description': 'd', 'comments': []}

    @patch('apps.integrations.briefing.AIClient.complete')
    def test_success_stores_summary_and_timestamp(self, mock_complete):
        from apps.integrations.briefing import refresh_claim_summary
        mock_complete.return_value = _fake_briefing('Fresh summary.')
        self.assertTrue(refresh_claim_summary(self.claim, self.ticket_data))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.ai_summary, 'Fresh summary.')
        self.assertIsNotNone(self.claim.ai_summary_updated_at)

    @patch('apps.integrations.briefing.AIClient.complete', side_effect=RuntimeError('AI down'))
    def test_failure_keeps_old_summary(self, mock_complete):
        from apps.integrations.briefing import refresh_claim_summary
        self.assertFalse(refresh_claim_summary(self.claim, self.ticket_data))
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.ai_summary, 'old text')
        self.assertIsNone(self.claim.ai_summary_updated_at)
```

- [ ] **Step 2: Run to verify failure** — `ModuleNotFoundError: apps.integrations.briefing`

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_briefing.py -q`

- [ ] **Step 3: Create `apps/integrations/briefing.py`** — move `ALF_BUSINESS_CONTEXT` here VERBATIM from `apps/integrations/views.py:339-354` and extend it:

```python
"""Shared AI business context + the claim summary engine.

The stored claim summary (claim.ai_summary) is written ONLY here — by the
Zendesk webhook (creation + status change) and the "Refresh from Zendesk"
view. The sidebar briefing endpoint shares the business context but stays
read-only (no stored-summary writes from agent clicks). All AI calls go
through apps/ai/AIClient (PII tokenization — never a passthrough)."""
import logging

from django.utils import timezone

from apps.ai.client import AIClient
from apps.ai.schemas import BriefingSummary
from apps.integrations.services import build_claim_facts, build_ticket_thread

logger = logging.getLogger(__name__)

STATUS_VOCABULARY = (
    "Zendesk workflow statuses (the claim's status uses these exact names): "
    "'New' and 'Open' = intake, not yet worked. 'Investigation initiated' = ALF staff "
    "working the case (client sees 'Open'). 'Claim submitted' = loss reports filed with "
    "the airport/airline/security institutions (client sees 'Search in progress'). "
    "'Object Found' = item located; retrieval or shipping underway. 'Pending' = waiting "
    "for the client to reply. 'Refund Requested' = client asked for a refund; management "
    "decision pending. 'Refund-Denied' = refund denied after confirming with the client; "
    "the case is closing. 'Solved' and 'Solved - Object Found' = case ended successfully. "
    "'Closed - Object Not Found' = search failed and the case is closed. "
    "'Closed - Client Not Answering' = closed because the client stopped responding. "
    "'Closed - Refunded' = closed with a refund. "
)

ALF_BUSINESS_CONTEXT = (
    # ... the existing 16-line string moved VERBATIM from views.py ...
) + STATUS_VOCABULARY

SUMMARY_PROMPT = ALF_BUSINESS_CONTEXT + (
    "Write a management summary of at most 4 sentences for this lost-item case. "
    "Lead with the current workflow status and what it means for the case, then "
    "the key facts (what was lost, where, search position), then what is "
    "currently awaited and from whom. Use ONLY facts present in the provided "
    "content; never invent dates, people or procedures. "
    'Respond as JSON: {"summary": "..."}.'
)


def normalize_fetched_comments(comments):
    """Server-fetched Zendesk comments ({author:{name}, body, public,
    created_at}) -> the dict shape build_ticket_thread renders."""
    normalized = []
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        author = c.get('author')
        if isinstance(author, dict):
            author = author.get('name', '')
        normalized.append({
            'author': str(author or ''),
            'created_at': str(c.get('created_at', '') or ''),
            'public': c.get('public', True),
            'text': str(c.get('body', '') or c.get('text', '') or ''),
        })
    return normalized


def generate_claim_summary(claim, ticket_data):
    """One AI summary of the case, or None on any AI failure (callers must
    treat the summary as optional — a stage change never depends on it)."""
    facts = build_claim_facts(claim)
    untrusted = build_ticket_thread({
        'subject': ticket_data.get('subject', ''),
        'description': ticket_data.get('description', ''),
        'ticket_created_at': ticket_data.get('created_at', ''),
        'comments': normalize_fetched_comments(ticket_data.get('comments')),
    })
    known_pii = {'aliases': [], 'names': [n for n in [claim.client_name] if n]}
    try:
        result = AIClient.complete(
            system_prompt=SUMMARY_PROMPT,
            trusted={'claim_facts': str(facts)},
            untrusted=untrusted,
            known_pii=known_pii,
            response_schema=BriefingSummary,
            call_site='claim_summary',
            temperature=0.4,
            max_tokens=500,
        )
    except Exception as e:
        logger.warning(f"Claim summary generation failed for claim #{claim.id}: {e}")
        return None
    return result.summary


def refresh_claim_summary(claim, ticket_data) -> bool:
    """Regenerate and store the claim's summary. True on success."""
    summary = generate_claim_summary(claim, ticket_data)
    if summary is None:
        return False
    claim.ai_summary = summary
    claim.ai_summary_updated_at = timezone.now()
    claim.save(update_fields=['ai_summary', 'ai_summary_updated_at'])
    return True
```

(The `# ...` line is the ONE intentional ellipsis in this plan: it means
"paste the existing string from views.py lines 339-354 unchanged" — it is a
move, not new writing. Do not retype it.)

- [ ] **Step 4: Point `views.py` at the moved constant** — in `apps/integrations/views.py` delete lines 339-354 (the `ALF_BUSINESS_CONTEXT = (...)` block) and add to the module imports:

```python
from apps.integrations.briefing import ALF_BUSINESS_CONTEXT, refresh_claim_summary
```

(The second name is used by Task 7; importing it now keeps one edit.)

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest apps/integrations/tests/test_briefing.py apps/integrations/tests/test_sidebar_ai_endpoints.py -q
```

Expected: new tests PASS; sidebar endpoint tests still PASS (context moved, prompts byte-identical apart from the appended vocabulary).

- [ ] **Step 6: Commit**

```bash
git add apps/integrations/briefing.py apps/integrations/views.py apps/integrations/tests/test_briefing.py
git commit -m "feat(integrations): claim summary engine + status vocabulary in shared business context"
```

---

### Task 7: Webhook — mandatory auth + status-change mirroring + real creation summary

**Files:**
- Modify: `apps/integrations/views.py` (`ZendeskClaimWebhookView`, lines 938-1228)
- Test: `apps/integrations/tests/test_zendesk_claim_webhook.py`

- [ ] **Step 1: Update existing tests for mandatory auth.** In `test_zendesk_claim_webhook.py`: in `setUp`, ensure a `SystemSettings` instance exists with `sidebar_secret_token='test-webhook-secret'` (follow the file's existing SystemSettings usage; add the field if the existing setUp creates one without it). Add a posting helper to the TestCase base and convert every `self.client.post(...)` in the file to use it:

```python
    def _post_webhook(self, payload):
        return self.client.post(
            self.webhook_url, payload, content_type='application/json',
            HTTP_X_WEBHOOK_SECRET='test-webhook-secret',
        )
```

Also update every assertion that expects created claims with `status='Received'` to `status='Investigation initiated'`.

- [ ] **Step 2: Add new failing tests** (append to the same file):

```python
from unittest.mock import patch


class WebhookAuthRequiredTests(TestCase):
    # reuse the file's existing setUp pattern (SystemSettings + webhook_url)

    def test_missing_secret_is_rejected(self):
        payload = {'event': {'current': '11688538967068'}, 'detail': {'id': '50001'}}
        response = self.client.post(self.webhook_url, payload, content_type='application/json')
        self.assertEqual(response.status_code, 401)

    def test_wrong_secret_is_rejected(self):
        payload = {'event': {'current': '11688538967068'}, 'detail': {'id': '50001'}}
        response = self.client.post(self.webhook_url, payload, content_type='application/json',
                                    HTTP_X_WEBHOOK_SECRET='wrong')
        self.assertEqual(response.status_code, 401)


class WebhookStatusMirrorTests(TestCase):
    # reuse the file's existing setUp pattern; create one claim:
    def setUp(self):
        super().setUp()  # or repeat the SystemSettings setup
        from apps.claims.models import Claim
        self.claim = Claim.objects.create(
            client_email='mirror@example.com', zd_ticket_id='60001',
            status='Investigation initiated', status_category='open')

    def _payload(self, status_id='222'):
        return {'event': {'current': status_id}, 'detail': {'id': '60001'}}

    @patch('apps.integrations.views.refresh_claim_summary', return_value=True)
    @patch('apps.integrations.views.resolve_custom_status',
           return_value={'name': 'Claim submitted', 'category': 'open'})
    @patch('apps.integrations.views.fetch_zendesk_comments', return_value=[])
    @patch('apps.integrations.views.fetch_zendesk_ticket',
           return_value={'subject': 's', 'description': 'd', 'comments': []})
    def test_status_change_updates_claim_and_writes_timeline(self, *_mocks):
        response = self._post_webhook(self._payload())
        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, 'Claim submitted')
        self.assertEqual(self.claim.status_category, 'open')
        self.assertIsNotNone(self.claim.status_changed_at)
        entry = self.claim.updates.first()
        self.assertEqual(entry.update_type, 'STATUS_CHANGE')
        self.assertIn('Investigation initiated', entry.changes_summary)

    @patch('apps.integrations.views.resolve_custom_status',
           return_value={'name': 'Investigation initiated', 'category': 'open'})
    def test_same_status_is_a_noop(self, _mock):
        before = self.claim.updated_at
        response = self._post_webhook(self._payload('111'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.claim.updates.count(), 0)

    @patch('apps.integrations.views.refresh_claim_summary', return_value=False)
    @patch('apps.integrations.views.resolve_custom_status',
           return_value={'name': 'Object Found', 'category': 'open'})
    @patch('apps.integrations.views.fetch_zendesk_comments', return_value=[])
    @patch('apps.integrations.views.fetch_zendesk_ticket', return_value=None)
    def test_summary_failure_does_not_block_status_update(self, *_mocks):
        response = self._post_webhook(self._payload())
        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, 'Object Found')
        entry = self.claim.updates.first()
        self.assertEqual(entry.llm_summary, '')

    def test_unknown_ticket_with_non_creation_status_is_ignored(self):
        with patch('apps.integrations.views.resolve_custom_status',
                   return_value={'name': 'Pending', 'category': 'pending'}):
            response = self._post_webhook(
                {'event': {'current': '333'}, 'detail': {'id': '99999'}})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Ignored', response.json()['message'])
```

(`WebhookStatusMirrorTests` needs the same `_post_webhook` helper — inherit from the file's base TestCase or repeat the helper.)

- [ ] **Step 3: Run to verify the new tests fail**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_zendesk_claim_webhook.py -q`
Expected: new tests FAIL (401 not enforced; status-change path missing); converted old tests PASS.

- [ ] **Step 4: Rewrite the view's entry flow.** In `apps/integrations/views.py`, ensure module-level imports include (add to the existing services import or create one):

```python
from apps.integrations.services import (
    fetch_zendesk_ticket, fetch_zendesk_comments, resolve_custom_status,
)
```

In `ZendeskClaimWebhookView.post`, replace everything from the `try:` down to the `# Validate status` block (the DEBUG logging at lines 983-988, payload parsing at 990-1001, the optional-secret check at 1010-1020, and the status gate at 1022-1028) with:

```python
        try:
            data = request.data

            # Auth is mandatory: a webhook without the correct shared secret
            # is rejected before anything is parsed or logged.
            webhook_secret = request.headers.get('X-Webhook-Secret', '')
            expected_secret = SystemSettings.get_instance().sidebar_secret_token or ''
            if not (webhook_secret and expected_secret
                    and hmac.compare_digest(webhook_secret, expected_secret)):
                logger.warning("Rejected Zendesk webhook: missing or invalid X-Webhook-Secret")
                return Response({'error': 'Invalid webhook secret'},
                                status=status.HTTP_401_UNAUTHORIZED)

            event_data = data.get('event', {})
            detail_data = data.get('detail', {})
            custom_status = str(event_data.get('current')
                                or detail_data.get('custom_status', '') or '')
            ticket_id = detail_data.get('id') or data.get('ticket_id')
            subject = detail_data.get('subject', '')

            if not ticket_id:
                logger.warning("Zendesk webhook missing ticket id")
                return Response({'error': 'Missing required field: ticket_id'},
                                status=status.HTTP_400_BAD_REQUEST)
            ticket_id = str(ticket_id)

            from apps.claims.models import Claim
            claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()

            if claim:
                return self._handle_status_change(claim, custom_status)

            if custom_status != self.INVESTIGATION_STATUS_ID:
                logger.info(
                    f"Ignoring webhook for ticket {ticket_id}: no claim and "
                    f"custom_status '{custom_status}' is not investigation initiated")
                return Response({
                    'message': 'Ignored: no claim and status is not investigation initiated',
                    'custom_status': custom_status,
                }, status=status.HTTP_200_OK)
```

The existing creation code continues from here (the old "Check if claim already exists" block at 1030-1040 is now redundant — DELETE it; the `claim = Claim.objects.filter(...)` above replaced it; the IntegrityError race handling deeper in the method STAYS).

- [ ] **Step 5: Add the status-change handler as a method on the view:**

```python
    def _handle_status_change(self, claim, custom_status_id):
        """Mirror a Zendesk custom-status change onto an existing claim and
        refresh the stored AI summary. The summary is best-effort: the stage
        update and history entry must land even when AI or the ticket fetch
        fails."""
        import json as json_module
        from django.utils import timezone as dj_timezone
        from apps.claims.models import ClaimUpdateTimeline

        if not custom_status_id:
            return Response({'message': 'Ignored: no custom status in payload'},
                            status=status.HTTP_200_OK)

        resolved = resolve_custom_status(custom_status_id)
        new_status = resolved['name']
        if new_status == claim.status:
            return Response({'message': 'No change', 'claim_id': claim.id,
                             'status': claim.status}, status=status.HTTP_200_OK)

        old_status = claim.status
        claim.status = new_status
        claim.status_category = resolved['category']
        claim.status_changed_at = dj_timezone.now()
        claim.save(update_fields=['status', 'status_category', 'status_changed_at'])
        logger.info(f"Claim #{claim.id} status mirrored: '{old_status}' -> '{new_status}'")

        summary_text = ''
        ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
        if ticket_data:
            ticket_data['comments'] = fetch_zendesk_comments(claim.zd_ticket_id)
            if refresh_claim_summary(claim, ticket_data):
                summary_text = claim.ai_summary

        ClaimUpdateTimeline.objects.create(
            claim=claim,
            zendesk_ticket_id=claim.zd_ticket_id or '',
            update_type='STATUS_CHANGE',
            changes_summary=json_module.dumps(
                {'old_status': old_status, 'new_status': new_status}),
            llm_summary=summary_text,
        )
        return Response({'message': 'Status updated', 'claim_id': claim.id,
                         'status': new_status}, status=status.HTTP_200_OK)
```

- [ ] **Step 6: Real summary at creation.** In the creation flow, delete the glue-text block (lines 1141-1154, `ai_summary_parts` ... `ai_summary = ' '.join(...)`) and change the `Claim.objects.create(...)` call: replace `status='Received',` with:

```python
                        status='Investigation initiated',
                        status_category='open',
                        status_changed_at=timezone.now(),
                        deadline_at=compute_deadline_at(
                            _safe_date(extracted_data.get('deadline_date', '')),
                            extracted_data.get('deadline_time', ''),
                            extracted_data.get('deadline_timezone', ''),
                        ),
```

and `ai_summary=ai_summary,` with `ai_summary='',`. Add the imports at module level: `from apps.claims.services import compute_deadline_at` and `from django.utils import timezone` (if not already imported). Immediately after the `except IntegrityError` block's normal path (after the claim is created, before the final log + 201 Response), add:

```python
            # Real AI summary (best-effort — creation never fails on AI)
            ticket_data['comments'] = comments
            refresh_claim_summary(claim, ticket_data)
```

(`comments` is already fetched a few lines above; `ticket_data['comments'] = comments` already exists at line 1060 — keep one of the two, do not duplicate.)

- [ ] **Step 7: Run the whole webhook test file**

Run: `.venv/bin/python -m pytest apps/integrations/tests/test_zendesk_claim_webhook.py -q`
Expected: ALL PASS. Existing creation tests may need `@patch('apps.integrations.views.refresh_claim_summary', return_value=False)` added where they don't mock AI — add it.

- [ ] **Step 8: Commit**

```bash
git add apps/integrations/views.py apps/integrations/tests/test_zendesk_claim_webhook.py
git commit -m "feat(integrations): webhook mirrors every Zendesk status change; mandatory secret; real creation summary"
```

---

### Task 8: Remove every LORA stage writer

**Files:**
- Modify: `apps/claims/views.py` (delete `update_status` action, lines 96-129)
- Modify: `apps/users/views.py` (delete `agent_update_status` lines 302-325; fix `status_choices` contexts at 234, 263, 648; dashboard stats at 130-140, 539-545, 585-590)
- Modify: `apps/users/urls.py:18` (delete route)
- Modify: `apps/payments/refund_service.py` (lines 114-119 and 363-369)
- Modify: `apps/integrations/views.py` (delete `ZendeskStatusWebhookView`, lines ~843-935) and `apps/integrations/urls.py:21`
- Tests: `apps/claims/tests/test_claim_model.py` or wherever `update_status`/status-webhook tests live (delete them); `apps/users/` tests if any reference `agent_update_status`

- [ ] **Step 1: Find every test that exercises the removed paths**

```bash
grep -rn "update_status\|agent_update_status\|status-webhook\|ZendeskStatusWebhookView\|refund_requested" apps/ --include="*.py" | grep -i test
```

Delete those test functions/classes (they test behavior that no longer exists). Note them in the commit message.

- [ ] **Step 2: Delete the writers**

1. `apps/claims/views.py`: remove the whole `@action ... def update_status` block (lines 96-129).
2. `apps/users/views.py`: remove `agent_update_status` (the `@agent_required @transaction.atomic def agent_update_status` block, lines 302-325).
3. `apps/users/urls.py`: remove line 18 (`path('agent/claims/<int:claim_id>/status/', ...)`).
4. `apps/payments/refund_service.py` lines 114-119 — delete:

```python
            # Update claim status
            if refund_type == 'FULL':
                claim.status = 'REFUNDED'
            else:
                claim.status = 'PARTIALLY_REFUNDED'
            claim.save()
```

5. Same file lines 363-369 — delete:

```python
            # Update claim status
            refund_type = 'FULL' if refund_amount >= self._get_claim_total(claim) else 'PARTIAL'
            if refund_type == 'FULL':
                claim.status = 'REFUNDED'
            else:
                claim.status = 'PARTIALLY_REFUNDED'
            claim.save()
```

6. `apps/integrations/views.py`: delete the entire `ZendeskStatusWebhookView` class (from `class ZendeskStatusWebhookView(APIView):` through its final `return Response(... 500 ...)`, lines ~843-935 — verify boundaries by reading the file).
7. `apps/integrations/urls.py`: remove line 21 and the `ZendeskStatusWebhookView` import.

- [ ] **Step 3: Replace the `status_choices` contexts.** `Claim.STATUS_CHOICES` no longer exists. In `apps/users/views.py` at the three context dicts (lines 234, 263, 648 — agent claim detail, agent claims list, manager claims list):

- At line 234 (claim detail context): DELETE the `'status_choices': Claim.STATUS_CHOICES,` line (the detail page dropdown goes away in Task 10).
- At lines 263 and 648 (list filters): replace with live values so the filter dropdowns keep working:

```python
        'status_choices': [
            (s, s) for s in Claim.objects.exclude(status='')
            .values_list('status', flat=True).distinct().order_by('status')
        ],
```

- [ ] **Step 4: Family-based dashboard stats.** In `apps/users/views.py`:

`agent_dashboard` (replace lines 131-140):

```python
    total_claims = Claim.objects.count()
    my_claims = Claim.objects.filter(
        assigned_to=request.user
    ).exclude(status_category='solved').count()
    urgent_emails = EmailLog.objects.filter(
        action_required=True,
        category__in=['RESUBMISSION_REQUIRED', 'OBJECT_NOT_FOUND']
    ).count()
    disputed = Claim.objects.filter(disputes__isnull=False).distinct().count()
```

`manager_dashboard` (replace the `stats = Claim.objects.aggregate(...)` block, lines 539-545):

```python
    stats = Claim.objects.aggregate(
        total=Count('id'),
        active=Count(Case(When(~Q(status_category='solved'), then=1),
                          output_field=IntegerField())),
        pending_client=Count(Case(When(status_category='pending', then=1),
                                  output_field=IntegerField())),
        solved=Count(Case(When(status_category='solved', then=1),
                          output_field=IntegerField())),
    )
```

Add `Q` to the `django.db.models` imports in that function. Replace the context keys (lines 586-590):

```python
        'total_claims': stats['total'],
        'active': stats['active'],
        'pending_client': stats['pending_client'],
        'solved': stats['solved'],
        'disputed': dispute_stats['total'] - dispute_stats['resolved'],
```

- [ ] **Step 5: Update the manager dashboard stat cards.** In `templates/manager/dashboard.html` find the stat cards using `{{ received }}`, `{{ searching }}`, `{{ found }}` (grep for each) and rename them:

- `{{ received }}` card → value `{{ active }}`, label text `Active cases`
- `{{ searching }}` card → value `{{ pending_client }}`, label text `Awaiting client`
- `{{ found }}` card → value `{{ solved }}`, label text `Solved`
- `{{ disputed }}` card keeps its variable (now open-dispute count); change its label to `Open disputes` if it says otherwise.

- [ ] **Step 6: Run affected tests + full suite checkpoint**

```bash
.venv/bin/python -m pytest apps/users apps/claims apps/payments apps/integrations -q
```

Expected: PASS (after Step 1 deletions). Investigate any failure before continuing.

- [ ] **Step 7: Commit**

```bash
git add -A ':!.gitignore'
git commit -m "feat: LORA stops writing claim stages — drop manual status endpoints, refund stamps, legacy status webhook; family-based dashboard counts"
```

---

### Task 9: Rebuild "Refresh from Zendesk"

**Files:**
- Modify: `apps/claims/views.py` (`ClaimUpdateFromZendeskView`, lines 236-440)
- Modify: `apps/integrations/views.py` + `apps/integrations/services.py` (relocate `_safe_date`/`_safe_decimal`)
- Delete: `apps/claims/update_zendesk_debug.log`
- Modify: `templates/agent/claim_detail.html` (button labels, lines 20 + 276)
- Test: create `apps/claims/tests/test_refresh_from_zendesk.py`

- [ ] **Step 1: Relocate the coercion helpers.** Move `_safe_date` and `_safe_decimal` from `apps/integrations/views.py` to `apps/integrations/services.py` VERBATIM (find them with `grep -n "_safe_date\|_safe_decimal" apps/integrations/views.py`), renamed public: `safe_date`, `safe_decimal`. Update the two call sites in `apps/integrations/views.py` (creation flow) to `safe_date`/`safe_decimal` imported from services. Run `.venv/bin/python -m pytest apps/integrations -q` — expected PASS.

- [ ] **Step 2: Write failing tests** — create `apps/claims/tests/test_refresh_from_zendesk.py`:

```python
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.claims.models import Claim
from apps.users.models import User


EXTRACTED = {
    'client_email': 'new@example.com', 'client_name': 'Ana Pop',
    'flight_details': 'RO301 2026-06-01 OTP-CDG', 'object_description': 'Black wallet',
    'phone': '+40712345678', 'alternate_email': '', 'claim_number': 'ALF1234567',
    'billing_address': '', 'shipping_address': 'Str. Lunga 1, Brasov',
    'incident_details': '', 'lost_location': 'Gate 12', 'deadline_date': '2026-07-01',
    'deadline_time': '17:00', 'deadline_timezone': 'CET', 'price_paid': '49.00',
    'payment_method': 'PayPal', 'payment_status': 'paid', 'woocommerce_id': '991',
    'tracking_info': '',
}


class RefreshFromZendeskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='agent1', password='x', role='AGENT')
        self.client_api = APIClient()
        self.client_api.force_authenticate(self.user)
        self.claim = Claim.objects.create(
            client_email='old@example.com', zd_ticket_id='70001',
            object_description='Existing description kept',
            flight_details='OLD FLIGHT')
        self.url = f'/api/claims/{self.claim.id}/update-from-zendesk/'

    def _run(self, refresh_ok=True):
        with patch('apps.claims.views.fetch_zendesk_ticket',
                   return_value={'subject': 'ALF1234567', 'description': 'd',
                                 'custom_fields': [], 'created_at': 'x'}), \
             patch('apps.claims.views.fetch_zendesk_comments', return_value=[]), \
             patch('apps.claims.views.analyze_zendesk_ticket_for_claim',
                   return_value=dict(EXTRACTED)), \
             patch('apps.claims.views.refresh_claim_summary',
                   return_value=refresh_ok):
            return self.client_api.post(self.url)

    def test_structured_fields_overwrite_and_fill_only_respected(self):
        response = self._run()
        self.assertEqual(response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.flight_details, 'RO301 2026-06-01 OTP-CDG')  # overwritten
        self.assertEqual(self.claim.object_description, 'Existing description kept')  # fill-only
        self.assertEqual(self.claim.shipping_address, 'Str. Lunga 1, Brasov')
        self.assertIsNotNone(self.claim.deadline_at)
        self.assertEqual(str(self.claim.price_paid), '49.00')

    def test_timeline_entry_written(self):
        self._run()
        entry = self.claim.updates.first()
        self.assertEqual(entry.update_type, 'INFO_UPDATED')
        self.assertIn('flight_details', entry.changes_summary)

    def test_requires_agent_or_manager(self):
        self.user.role = 'CLIENT'
        self.user.save()
        response = self._run()
        self.assertEqual(response.status_code, 403)
```

(Adjust `User.objects.create_user` kwargs to the project's user model — check `apps/users/models.py` for required fields; existing tests in the repo show the pattern.)

- [ ] **Step 3: Run to verify failure** — the patch targets don't exist yet on `apps.claims.views`:

Run: `.venv/bin/python -m pytest apps/claims/tests/test_refresh_from_zendesk.py -q`
Expected: FAIL

- [ ] **Step 4: Rewrite the view.** Replace the ENTIRE body of `ClaimUpdateFromZendeskView` in `apps/claims/views.py` (keep the class name and URL). Add module-level imports so tests can patch them:

```python
from apps.integrations.services import (
    fetch_zendesk_ticket, fetch_zendesk_comments,
    analyze_zendesk_ticket_for_claim, safe_date, safe_decimal,
)
from apps.integrations.briefing import refresh_claim_summary
from apps.claims.services import compute_deadline_at
```

```python
class ClaimUpdateFromZendeskView(APIView):
    """POST /api/claims/{claim_id}/update-from-zendesk/

    Re-extracts ALL claim facts from the live ticket and regenerates the AI
    summary. Values read from structured Zendesk fields overwrite the claim
    (Zendesk is the source of truth); LLM-inferred values fill blanks only."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    OVERWRITE_FIELDS = [
        'client_email', 'client_name', 'flight_details', 'phone',
        'billing_address', 'shipping_address', 'incident_details',
        'lost_location', 'deadline_time', 'deadline_timezone',
        'payment_method', 'payment_status', 'woocommerce_id', 'tracking_info',
    ]
    FILL_ONLY_FIELDS = ['object_description', 'alternate_email']

    def post(self, request, claim_id):
        if not hasattr(request.user, 'role') or request.user.role not in ['AGENT', 'MANAGER']:
            return Response({'error': 'Permission denied: AGENT or MANAGER role required'},
                            status=status.HTTP_403_FORBIDDEN)

        claim = get_object_or_404(Claim, id=claim_id)
        if not claim.zd_ticket_id:
            return Response({'error': 'No Zendesk ticket linked to this claim'},
                            status=status.HTTP_400_BAD_REQUEST)

        ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
        if not ticket_data:
            return Response({'error': 'Failed to fetch Zendesk ticket'},
                            status=status.HTTP_502_BAD_GATEWAY)
        ticket_data['comments'] = fetch_zendesk_comments(claim.zd_ticket_id)

        extracted = analyze_zendesk_ticket_for_claim(ticket_data)

        updated_fields = []
        for field in self.OVERWRITE_FIELDS:
            value = (extracted.get(field) or '').strip()
            if value and value != (getattr(claim, field) or ''):
                setattr(claim, field, value)
                updated_fields.append(field)
        for field in self.FILL_ONLY_FIELDS:
            value = (extracted.get(field) or '').strip()
            if value and not (getattr(claim, field) or ''):
                setattr(claim, field, value)
                updated_fields.append(field)

        new_date = safe_date(extracted.get('deadline_date', ''))
        if new_date and new_date != claim.deadline_date:
            claim.deadline_date = new_date
            updated_fields.append('deadline_date')
        new_price = safe_decimal(extracted.get('price_paid', ''))
        if new_price is not None and new_price != claim.price_paid:
            claim.price_paid = new_price
            updated_fields.append('price_paid')

        claim.deadline_at = compute_deadline_at(
            claim.deadline_date, claim.deadline_time, claim.deadline_timezone)
        claim.save()

        summary_refreshed = refresh_claim_summary(claim, ticket_data)

        ClaimUpdateTimeline.objects.create(
            claim=claim,
            zendesk_ticket_id=claim.zd_ticket_id,
            update_type='INFO_UPDATED',
            changes_summary=json.dumps({'updated_fields': updated_fields}),
            llm_summary=claim.ai_summary if summary_refreshed else '',
        )
        logger.info(f"Refreshed claim #{claim.id} from Zendesk: {updated_fields}")
        return Response({
            'message': 'Claim refreshed from Zendesk',
            'updated_fields': updated_fields,
            'summary_refreshed': summary_refreshed,
        })
```

Ensure `get_object_or_404` (from `django.shortcuts`), `json`, and `ClaimUpdateTimeline` are imported in the module. Delete the old imports the removed code used and the entire debug-log machinery.

- [ ] **Step 5: Remove the debug log file**

```bash
git rm apps/claims/update_zendesk_debug.log 2>/dev/null || rm -f apps/claims/update_zendesk_debug.log
```

Add `update_zendesk_debug.log` cleanup note: the new view writes no files.

- [ ] **Step 6: Rename the buttons.** In `templates/agent/claim_detail.html` lines 20 and 276 change `Update from Zendesk` → `Refresh from Zendesk` (the `updateFromZendesk(...)` JS function name and endpoint stay).

- [ ] **Step 7: Run tests**

```bash
.venv/bin/python -m pytest apps/claims -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add -A ':!.gitignore'
git commit -m "feat(claims): Refresh from Zendesk — full re-extraction (structured overwrites, LLM fills blanks) + real summary"
```

---

### Task 10: Templates, facts, and freshness

**Files:**
- Modify: `templates/agent/claim_detail.html` (status card 31-128; AI summary card ~237)
- Modify: `templates/agent/dashboard.html:205`, `templates/manager/dashboard.html:188`
- Modify: `apps/users/views.py` (claim detail context, ~line 255-266)
- Modify: `apps/integrations/services.py` (`build_claim_facts`)
- Test: `apps/integrations/tests/test_zendesk_services.py` (facts tests)

- [ ] **Step 1: Failing tests for facts** (append to `apps/integrations/tests/test_zendesk_services.py`, following its existing `build_claim_facts` test pattern):

```python
class BuildClaimFactsFamilyTests(TestCase):
    def test_status_family_included_and_cadence_suppressed_when_solved(self):
        from apps.integrations.services import build_claim_facts
        from apps.claims.models import Claim
        claim = Claim.objects.create(
            client_email='facts@example.com',
            status='Closed - Refunded', status_category='solved')
        facts = build_claim_facts(claim)
        self.assertEqual(facts['status'], 'Closed - Refunded')
        self.assertEqual(facts['status_family'], 'solved')
        self.assertIsNone(facts['next_update_due'])

    def test_active_claim_keeps_cadence(self):
        from apps.integrations.services import build_claim_facts
        from apps.claims.models import Claim
        claim = Claim.objects.create(
            client_email='facts2@example.com',
            status='Claim submitted', status_category='open')
        facts = build_claim_facts(claim)
        self.assertIsNotNone(facts['next_update_due'])
```

- [ ] **Step 2: Run to verify failure** — `KeyError: 'status_family'`

- [ ] **Step 3: Update `build_claim_facts`** in `apps/integrations/services.py`: compute the cadence only for unsolved claims and add the family:

```python
    next_update_due = None
    if claim.status_category != 'solved':
        base = timezone.localtime(claim.created_at).date()
        today = timezone.localdate()
        for day in CLIENT_UPDATE_CADENCE_DAYS:
            due = base + timedelta(days=day)
            if due >= today:
                next_update_due = {'day': day, 'date': due.isoformat()}
                break

    deadline = None
    if claim.deadline_at:
        deadline = timezone.localtime(claim.deadline_at).date().isoformat()
    elif claim.deadline_date:
        deadline = claim.deadline_date.isoformat()

    return {
        'status': claim.status,
        'status_family': claim.status_category,
        'deadline': deadline,
        'emails_total': emails.count(),
        'emails_unresolved': emails.filter(action_required=True, auto_resolved=False).count(),
        'disputes_total': Dispute.objects.filter(claim=claim).count(),
        'next_update_due': next_update_due,
    }
```

- [ ] **Step 4: Claim detail status card.** In `templates/agent/claim_detail.html` replace lines 37-127 (big status card + overview rows + grant-refund condition + update form) with:

```html
            <!-- Primary Status Badge - mirrors the Zendesk ticket -->
            <div class="status-card-large family-{{ claim.status_category|default:'open' }} mb-4">
                <div class="status-icon">
                    {% if claim.status_category == 'solved' %}
                        <i class="bi bi-check-circle"></i>
                    {% elif claim.status_category == 'pending' %}
                        <i class="bi bi-hourglass-split"></i>
                    {% elif claim.status_category == 'hold' %}
                        <i class="bi bi-pause-circle"></i>
                    {% elif claim.status_category == 'new' %}
                        <i class="bi bi-inbox"></i>
                    {% else %}
                        <i class="bi bi-search"></i>
                    {% endif %}
                </div>
                <div class="status-text">
                    <span class="status-label">{{ claim.status }}</span>
                    {% if claim.status_changed_at %}
                    <span class="text-xs text-base-content/50 block">since {{ claim.status_changed_at|date:"M d, Y H:i" }}</span>
                    {% endif %}
                </div>
            </div>

            <!-- Active Statuses Overview -->
            <div class="space-y-2">
                <span class="text-xs text-base-content/60 font-semibold uppercase tracking-wider">Status Overview</span>

                <div class="flex items-center justify-between p-2 rounded-lg bg-base-200/50">
                    <span class="text-sm flex items-center gap-2">
                        <i class="bi bi-box-seam text-base-content/60"></i> Case
                    </span>
                    <span class="badge badge-sm {% if claim.status_category == 'solved' %}badge-neutral{% else %}badge-success{% endif %}">
                        {% if claim.status_category == 'solved' %}Closed{% else %}Active{% endif %}
                    </span>
                </div>

                <div class="flex items-center justify-between p-2 rounded-lg bg-base-200/50">
                    <span class="text-sm flex items-center gap-2">
                        <i class="bi bi-currency-dollar text-base-content/60"></i> Financial
                    </span>
                    <span class="badge badge-sm {% if claim.refund_status == 'COMPLETED' %}badge-success{% elif claim.refund_status %}badge-warning{% else %}badge-neutral{% endif %}">
                        {% if claim.refund_status == 'COMPLETED' %}Refunded{% elif claim.refund_status %}{{ claim.refund_status|title }}{% else %}No Action{% endif %}
                    </span>
                </div>

                <div class="flex items-center justify-between p-2 rounded-lg bg-base-200/50">
                    <span class="text-sm flex items-center gap-2">
                        <i class="bi bi-exclamation-triangle text-base-content/60"></i> Dispute
                    </span>
                    <span class="badge badge-sm {% if claim.disputes.all %}badge-error{% else %}badge-neutral{% endif %}">
                        {% if claim.disputes.all %}Active Dispute{% else %}No Dispute{% endif %}
                    </span>
                </div>
            </div>

            <!-- Refund Action Button -->
            {% if claim.refund_status != 'COMPLETED' %}
            <div class="mt-4 pt-4 border-t border-base-200/60">
                <button type="button" class="btn btn-warning rounded-xl w-full transition-all duration-200" onclick="openGrantRefundModal()">
                    <i class="bi bi-currency-exchange"></i> Grant Refund
                </button>
            </div>
            {% endif %}

            <!-- Stage follows Zendesk -->
            <div class="mt-4 pt-4 border-t border-base-200/60">
                <p class="text-xs text-base-content/50 mb-2">
                    <i class="bi bi-arrow-repeat"></i> Status follows the Zendesk ticket automatically.
                </p>
                {% if claim.zd_ticket_id and zd_subdomain %}
                <a href="https://{{ zd_subdomain }}.zendesk.com/agent/tickets/{{ claim.zd_ticket_id }}"
                   target="_blank" rel="noopener"
                   class="btn btn-outline rounded-xl w-full transition-all duration-200">
                    <i class="bi bi-box-arrow-up-right"></i> Open ticket in Zendesk
                </a>
                {% endif %}
            </div>
```

(The view already passes `zd_subdomain`; `status_choices` was dropped from its context in Task 8.)

- [ ] **Step 5: AI summary freshness.** In the same template, in the AI summary card (~line 237), after the `{{ claim.ai_summary }}` span add:

```html
                {% if claim.ai_summary_updated_at %}
                <div class="text-xs text-base-content/40 mt-1">Updated {{ claim.ai_summary_updated_at|timesince }} ago</div>
                {% endif %}
```

- [ ] **Step 6: Dashboard badges.** Replace the status badge `div` in `templates/agent/dashboard.html:205` with:

```html
                        <div class="badge {% if claim.status_category == 'solved' %}badge-neutral{% elif claim.status_category == 'pending' %}badge-warning{% elif claim.status_category == 'new' %}badge-info{% elif claim.status_category == 'hold' %}badge-ghost{% else %}badge-primary{% endif %}">
                            {{ claim.status }}
                        </div>
```

And in `templates/manager/dashboard.html:188` the same with `badge-soft-*` variants:

```html
                            <div class="badge {% if claim.status_category == 'solved' %}badge-soft-neutral{% elif claim.status_category == 'pending' %}badge-soft-warning{% elif claim.status_category == 'new' %}badge-soft-info{% elif claim.status_category == 'hold' %}badge-soft-ghost{% else %}badge-soft-primary{% endif %} rounded-full">
                                {{ claim.status }}
                            </div>
```

- [ ] **Step 7: Run tests + template smoke**

```bash
.venv/bin/python -m pytest apps/integrations/tests/test_zendesk_services.py apps/users -q
.venv/bin/python manage.py check
```

Expected: PASS; `System check identified no issues`.

- [ ] **Step 8: Commit**

```bash
git add -A ':!.gitignore'
git commit -m "feat(ui): family-driven status display, read-only stage with Zendesk link, summary freshness, facts gain status_family"
```

---

### Task 11: Sweep — seed data, leftover references, full suite

**Files:**
- Modify: `apps/core/management/commands/seed_test_data.py:182` (old statuses)
- Modify: anything the greps below surface

- [ ] **Step 1: Hunt leftovers**

```bash
grep -rn "'Received'\|'Searching'\|'Found'\|'Shipped'\|'Disputed'\|REFUND_REQUESTED\|PARTIALLY_REFUNDED\|STATUS_CHOICES" apps/ templates/ --include="*.py" --include="*.html" | grep -v migrations | grep -v legacy_status_map | grep -v "Refund.STATUS_CHOICES" | grep -v "refund.status\|update.status\|dispute"
```

For each hit: claim-status references switch to the new vocabulary or family checks. Known targets: `seed_test_data.py` (use `['Investigation initiated', 'Claim submitted', 'Object Found', 'Closed - Object Not Found', 'Solved - Object Found']` with matching `status_category` values `['open', 'open', 'open', 'solved', 'solved']`); check `apps/agent/services.py` for status-keyword filters and update the same way. Dispute/Refund model statuses (RECEIVED etc.) are NOT claim statuses — leave them.

- [ ] **Step 2: Full suite**

```bash
.venv/bin/python -m pytest apps -q
```

Expected: ALL PASS (record the count). Fix anything red before continuing — no skips, no xfails.

- [ ] **Step 3: Commit**

```bash
git add -A ':!.gitignore'
git commit -m "chore: align seed data and remaining references with the Zendesk status vocabulary"
```

---

### Task 12: Docs, push, rollout checklist

**Files:**
- Modify: `docs/DEPLOYMENT.md` (§11 area), `zendesk_app/README.md` (facts note)
- Modify: `docs/superpowers/specs/2026-06-11-claim-entity-redesign-design.md` (status → Implemented)

- [ ] **Step 1: Docs.** In `docs/DEPLOYMENT.md` §11 add one paragraph: claims now mirror Zendesk custom statuses via the claim webhook (mandatory `X-Webhook-Secret`); the old `zd/status-webhook/` endpoint was removed. In `zendesk_app/README.md` "What the panel does", note facts now include the status family and that cadence stops on solved cases. Mark the spec header `Status: Implemented`.

- [ ] **Step 2: Push**

```bash
.venv/bin/python -m pytest apps -q && git add -A ':!.gitignore' && git commit -m "docs: status mirror + summary engine as-built notes" && git push
```

- [ ] **Step 3: Walk the user through rollout (interactive — present as a checklist):**

1. Admin → Config → System settings: `zd_subdomain` = `airportlf`, `zd_email`, `zd_token` filled (also fixes claim creation — already pending from earlier).
2. Zendesk Admin Center → Apps and integrations → Webhooks: the webhook pointing at `https://alfapp-production.up.railway.app/api/integrations/zd/claim-webhook/` sends header `X-Webhook-Secret` = the same value as LORA's `sidebar_secret_token`. Confirm no webhook targets `/zd/status-webhook/`.
3. Railway: confirm migrations run on deploy (service → Settings → Deploy: a pre-deploy/release command `python manage.py migrate`, or run it once via Railway shell after this deploy).
4. Smoke test: flip a test ticket through Investigation initiated → Claim submitted → Object Found → a Closed status; after each flip check the claim in LORA (status + family badge + timeline entry + summary refresh timestamp).

- [ ] **Step 4: Close out** — after rollout verification, run the user-requested **thorough code review** of the claims area (`/code-review` over the round's commits, or superpowers:requesting-code-review), and fix what it finds before calling the round done.
