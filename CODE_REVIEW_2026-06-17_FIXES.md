# Code Review 2026-06-17 — Fixes Applied

Companion to `CODE_REVIEW_2026-06-17.md`. Constraint for this pass: **fix the
findings without changing intended application logic** — apply behavior-preserving
cleanups everywhere, apply safety guards that protect existing intent, and
**leave (and flag) anything that is a deliberate design decision or that can't be
made provably behavior-preserving.**

**Verification:** `manage.py check` clean · `makemigrations --check` = no changes ·
full suite **1091 passed, 2 skipped** (baseline was 1085; +6 new regression tests).

---

## Phase A — behavior-preserving cleanups (applied across all 9 apps)

Done by category, values kept byte-identical, no control-flow changes:

- **Magic numbers/strings → named constants** (same value): Zendesk/IMAP/PayPal
  timeouts, flight-history window, LLM context caps, AI temperature/max_tokens,
  evidence upload limits (size/extensions/MIME/sniff bytes), page sizes, the
  "stuck"/deadline day thresholds, agent context-window slices, ciphertext
  inflation factors, `DEFAULT_STATUS`/`DEFAULT_CATEGORY`, etc.
- **Status/category literals → existing model choice constants** where an
  identical-value constant existed (`Dispute.STATUS_*`, `DisputeActivityLog.ACTION_*`,
  `EmailLog.CATEGORY_*`, `Refund.STATUS_*`).
- **Type hints + docstrings** on service/helper functions.
- **Stale AGENT/MANAGER docstrings** corrected to the single-authenticated-user model
  (kept the intentional `IsAgentOrManager`/`IsManager` aliases).
- **f-string logging → lazy `%`-style** (output identical, `exc_info` preserved).
- **Dead code / unused imports** removed (`import socket`, unused `QuerySet`,
  redundant local `Q` import, unused `EncryptedTextField` import, etc.).
- **`default='disconnected'` → `default=STATUS_DISCONNECTED`** (no migration).
- Exception chaining (`raise CommandError(...) from e`) in the dev seed command.

One Phase-A change was **reverted**: removing the inner
`from apps.integrations.services import ...` in `document_service._fetch_zendesk_ticket_full`
broke a test whose mock patches that call-time lookup. The inner import is a
deliberate testability seam, not dead duplication — restored.

---

## Phase B — safety guards applied (behavior changes that protect existing intent)

| # | Fix | File |
|---|-----|------|
| 1 | **`accept_claim` idempotency** — skip the POST if already ACCEPTED/RESOLVED (no double refund) + stable `PayPal-Request-Id` | `apps/payments/paypal_disputes_service.py` |
| 2 | **`provide_evidence` idempotency** — skip if already EVIDENCE_SENT + `PayPal-Request-Id` | `apps/payments/paypal_disputes_service.py` |
| 3 | **Client names redacted to the LLM** — register `client_name` in `known_pii["names"]` so the tokenizer redacts it before the prompt leaves the trust boundary | `apps/agent/services.py` |
| 4 | **Refund webhook idempotency gate** — `ProcessedWebhookEvent` claim-before-side-effects + release-on-failure (mirrors the dispute webhook) | `apps/payments/views.py` |
| 5 | **Refund webhook TOCTOU** — wrap the create in a savepoint + adopt-existing on `IntegrityError` (mirrors the WooCommerce path) | `apps/payments/refund_service.py` |
| 6 | **`toggle_setting_flag` race** — atomic `.filter(pk=1).update(...)` instead of re-saving the whole singleton | `apps/config/api/views.py` |
| 7 | **`toggle_service` race** — targeted single-column `.update(is_enabled=...)` | `apps/config/api/views.py` |
| 8 | **Scheduler master-switch race** — `save(update_fields=['is_enabled'])` | `apps/config/services/scheduler_controller.py` |
| 9 | **`manager_settings` atomicity** — wrap the two-write sequence in `transaction.atomic()` | `apps/users/views.py` |
| 10 | **Logout CSRF** — `@require_POST` on the view + the sidebar link converted to a POST form with CSRF token | `apps/users/views.py`, `templates/base.html` |
| 11 | **SSRF (scheme guard)** — reject non-http(s) probe URLs before requesting (AI + WooCommerce testers) | `apps/config/services/connection_tester.py` |

New regression tests: `apps/payments/tests/test_dispute_idempotency_guards.py`,
`apps/agent/tests/test_pii_names.py`, and a logout-GET-rejected test in
`apps/users/tests/test_views.py`.

---

## Deferred — left intentionally, with reasons

### Design decisions (do NOT "fix" without an explicit call)
- **`send_follow_up` posts before writing SENT state** (`communications/client_updates.py`).
  The code documents this as an "ACCEPTED RISK". Untouched.
- **`test_all_services` omits WOOCOMMERCE** (`config/services/connection_tester.py`).
  WooCommerce is a pending/not-fully-live integration (Wave 2, awaiting creds), so
  excluding it from the bulk health check is plausibly deliberate — not changed.
- **SSRF private-IP / metadata blocking** — deliberately **not** added. This is a
  single-trusted-operator, self-hostable tool where an AI/store endpoint on a
  private network is a legitimate config; blocking it would break real setups.
  Only the (harmless) scheme guard was added.

### Risky / can't be made provably behavior-preserving (need a deliberate session)
- **Scheduler run-lock** (`core/management/commands/run_scheduled_jobs.py`, HIGH).
  A correct lock is DB-specific (`pg_try_advisory_lock` in prod; sqlite in
  dev/tests has no equivalent) and a naive ServiceStatus "running" flag can wedge
  the whole cron if a run crashes. Individual jobs already carry their own
  idempotency (e.g. email Message-ID dedup). Recommend: Postgres advisory lock
  acquired at dispatcher start, released in a `finally`.
- **"Move logic into a service" thin-view refactors** (integrations, users, payments,
  claims). Pure structural moves with real behavior-change risk and no functional
  benefit — out of scope under "don't modify application logic".
- **Shared HTTP-helper extractions** (PayPal ×3, Zendesk `_zendesk_request`).
  Large refactors of live request/error handling.
- **N+1 query refactors** (`claims.refund_*` properties, `get_evidence_count`,
  `integrations` per-claim fetches). Touch querysets/viewsets — performance, not
  correctness; deferred to avoid altering result sets.
- **`AuthenticationForm`/`UserCreationForm` migration**, **login rate-limit
  hardening** (changes auth behavior; low confidence given internal scope),
  **blank-`message_id` ingestion change**, **`auto_now_add` on `last_checked`**
  (needs a migration), **agent `all_claim_ids` ID-space fix**, **claims cross-app
  status-string coupling**. Each changes runtime behavior in a way that warrants
  an explicit decision.

### Lower-value items left as-is
- `RESPONSE_LETTER`/unused `DisputeDocument` choices (removal needs a migration).
- `_render_to_pdf` public exposure; `_get_local_dispute` helper extraction;
  `build_dispute_evidence_bundle` split — cosmetic.
- The connection-tester `test_*` boilerplate dedup; `get_masked_value` dead-ish
  method (kept — referenced by tests).
