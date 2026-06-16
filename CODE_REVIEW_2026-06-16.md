# LORA — Django Code Review (10-dimension)

**Date:** 2026-06-16  ·  **Scope:** `apps/*` (9 apps) + `lora_app/` project config, ~17.8k LOC
**Method:** 18 parallel reviewers (full 10-dimension pass per file-group) → adversarial verification of every HIGH/MEDIUM finding against the live code → completeness-critic pass → second adversarial verification of the critic's gaps. Severity calibrated to the actual threat model: a single trusted-staff user type (no multi-tenancy), public webhooks must be signature/secret-verified, LLM PII boundary only.

## Result: 0 HIGH · 10 MEDIUM · 135 LOW

No HIGH-severity issues survived verification. Several plausible HIGHs (double-refund via `@retry`, frozen `last_checked` via `auto_now_add`, Zendesk-search injection) were **refuted** by reading the actual control flow — see _Evaluated and dismissed_. This is a mature codebase that has already absorbed multiple review passes; the remaining MEDIUMs are real but bounded, and the 135 LOWs are polish/maintainability/defense-in-depth.

## Summary table

| App | HIGH | MEDIUM | LOW | Top concern |
|-----|------|--------|-----|-------------|
| agent | 0 | 0 | 9 | Minor: type hints, magic strings, thin-view nits |
| ai | 0 | 1 | 8 | LLM client has no request timeout — a hung provider can exhaust gunicorn workers |
| claims | 0 | 1 | 8 | Re-extraction merge logic lives in the view, not a service |
| communications | 0 | 0 | 14 | Mostly polish — magic strings, long functions, a few N+1s |
| config | 0 | 1 | 9 | Single-key field encryption: rotating SECRET_KEY silently destroys stored credentials |
| core | 0 | 0 | 6 | Scheduler dispatcher has no run-lock (verified harmless — jobs are individually overlap-safe) |
| integrations | 0 | 1 | 19 | 240-line Zendesk webhook view mixing HTTP + extraction + claim creation (no bug, altitude) |
| lora_app | 0 | 1 | 8 | Dev venv (Django 4.2.29) != pinned prod (5.2.14); a few settings hygiene items |
| payments | 0 | 4 | 44 | Refund/dispute idempotency & over-refund cap on the PayPal-direct path; in-flight lock can wedge a dispute |
| users | 0 | 1 | 10 | Login throttle keyed on proxy IP (one global bucket); evidence-upload validation duplicated in the view |
| **TOTAL** | **0** | **10** | **135** | |

---

## MEDIUM-severity findings (10)

### M1. apps/payments/refund_service.py
```
[SEVERITY: MEDIUM]
File: apps/payments/refund_service.py
Line: 464-525 (existence check at 464-467; create at 512-525; broad except that swallows IntegrityError at 536-541)
Principle: IDEMPOTENCY (5)
Issue: process_woocommerce_refund does check-then-create (filter existing -> create) with no atomic/unique-guard around it, so two concurrent webhook retries can both pass the existence check and the second create() raises IntegrityError on the unique paypal_refund_id, surfacing as a generic 500/error instead of an idempotent success.
Fix: Wrap the reconcile+create in transaction.atomic() and catch IntegrityError specifically: on conflict, re-fetch Refund.objects.get(paypal_refund_id=f'WC-{refund_id}') and return it with already_processed=True (idempotent success), mirroring the dispute webhook's atomic-claim pattern. Equivalently, replace the create() with get_or_create(paypal_refund_id=f'WC-{refund_id}', defaults={...}) and key success/already_processed off the created flag. Also narrow the broad except so IntegrityError on the unique key is not collapsed into a generic {'success': False} error.
```

### M2. apps/payments/refund_service.py
```
[SEVERITY: MEDIUM]
File: apps/payments/refund_service.py
Line: 42-145 (row created at lines 88-99 with no cap); contrast issue_woocommerce_refund cap at lines 352-364
Principle: IDEMPOTENCY (5)
Issue: The PayPal-direct initiate_refund has no over-refund cap and no row-locked reservation against the claim's remaining refundable amount (price_paid minus already-reserved), unlike issue_woocommerce_refund which carefully enforces this; two PayPal refunds for the same claim can exceed price_paid.
Fix: Extract the cap+reservation logic from issue_woocommerce_refund into a shared helper, e.g. _reserve_refund(claim, amount) that, inside transaction.atomic(): does Claim.objects.select_for_update().get(pk=claim.pk), aggregates refunds.filter(status__in=RESERVING_STATUSES) Sum('amount'), and returns an error if amount > price_paid - reserved (else creates/returns the PENDING row). Call this helper in initiate_refund BEFORE _process_paypal_refund, replacing the bare Refund.objects.create at lines 88-99, so the over-refund cap and row-locked reservation apply uniformly to both the PayPal-direct and WooCommerce paths. As defense in depth, also add the same cap check to RefundCreateSerializer.validate (it currently only enforces amount > 0).
```

### M3. apps/payments/frontend_views.py
```
[SEVERITY: MEDIUM]
File: apps/payments/frontend_views.py
Line: dispute_accept_claim CAS 956-959, finally 1003-1005; dispute_manual_reply CAS 891-893, finally 907 (model field models.py:274)
Principle: A persisted in-flight/lock flag with no TTL, timestamp, or out-of-band reset can wedge a record permanently if the process dies between acquire and release.
Issue: outbound_in_flight is a plain DB boolean set True via CAS before the PayPal call and cleared only in a Python finally block, so a SIGKILL/OOM mid-call leaves it stuck True with no automated, admin, or management-command way to clear it, permanently blocking accept-claim and manual-reply on that dispute.
Fix: Make the lock self-healing: store an outbound_in_flight_at timestamp alongside the flag and treat the lock as stale after a short TTL (e.g. allow the CAS to also win when in_flight=True AND in_flight_at < now - N minutes), and/or expose outbound_in_flight as an editable/readonly-with-reset-action field in DisputeAdmin so staff can clear a wedged dispute without a DB shell.
```

### M4. apps/payments/document_service.py and templates/dispute_response_letter.html
```
[SEVERITY: MEDIUM]
File: apps/payments/document_service.py and templates/dispute_response_letter.html
Line: document_service.py:554 (f-string build), :588 (content_html store), :28-29 (orphaned allowlist); dispute_response_letter.html:243 (bare render); frontend_views.py:543-556 (edit-save sanitize + EVIDENCE_REPORT-only re-render)
Principle: Output rendering fidelity / consistent sanitization across all write-then-render paths
Issue: The AI letter body is built as plain text with \n\n separators and rendered through Django-autoescaped {{ ai_generated_content }} with no |linebreaks and no white-space CSS, so newlines collapse into one run-on paragraph (and any LLM-emitted HTML shows as escaped literal tags) in the PDF submitted to PayPal; meanwhile sanitization differs per path and the document_service ALLOWED_HTML_* constants are dead.
Fix: In dispute_response_letter.html line 243 render the body with {{ ai_generated_content|linebreaks }} (or set white-space: pre-line on .response-body) so paragraph breaks survive — matching how line 280 already renders comment.body with |escape|linebreaks. Separately, either delete the unused ALLOWED_HTML_TAGS/ATTRIBUTES in document_service.py:28-29 or route both generate and edit paths through one shared sanitizer, and extend the edit-save PDF re-render (frontend_views.py:556) to RESPONSE_LETTER docs so an edited response letter's submitted PDF isn't stale.
```

### M5. apps/ai/client.py
```
[SEVERITY: MEDIUM]
File: apps/ai/client.py
Line: 78-80 (_build_openai_client), 137-142 (complete's chat.completions.create)
Principle: Synchronous outbound calls to third-party services on the request path must have a bounded timeout, or a slow/hung dependency exhausts the worker pool and takes down availability.
Issue: The OpenAI client is built and called with no per-request timeout and no max_retries override, so a hung LLM provider can occupy a gunicorn worker for up to ~600s (the SDK default) per call, and a burst of synchronous sidebar/chat requests can exhaust the worker pool.
Fix: Pass an explicit timeout (e.g. 30-60s) and a small max_retries to the client: OpenAI(api_key=..., base_url=..., timeout=30.0, max_retries=1) in _build_openai_client(); optionally also pass timeout per-call via client.with_options(timeout=...). Source the value from SystemSettings/env so it's tunable. This caps worst-case worker occupancy and prevents pool exhaustion.
```

### M6. apps/users/views.py
```
[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 57, 61, 86, 93 (throttle key); lora_app/settings.py:201 (unused 'login' DRF scope)
Principle: Per-actor throttling must key on the real client identity; behind a reverse proxy REMOTE_ADDR is the proxy, collapsing all clients into one bucket and making the limiter both globally lockable and trivially evadable.
Issue: The login throttle keys on REMOTE_ADDR which, behind Railway's proxy, is the proxy IP, so every staff member shares one global login_attempts bucket — and the DRF 'login' 5/min scope defined in settings is never wired to login_view.
Fix: Derive the client IP from the proxy's forwarded header (e.g. left-most untrusted hop of HTTP_X_FORWARDED_FOR, or django-ipware) and use that in the cache key, and/or rate-limit per-username instead of per-IP for the credential boundary; delete the dead 'login' DRF scope or wire it to a DRF login endpoint so it isn't mistaken for active protection.
```

### M7. apps/config/encrypted_fields.py
```
[SEVERITY: MEDIUM]
File: apps/config/encrypted_fields.py
Line: _get_fernet derivation L17-38 (esp. L23, L28); from_db_value error-swallowing L67-76 and L96-105; destructive re-save path apps/users/views.py L987 + apps/config/models.py save() L393-400
Principle: Don't couple at-rest credential encryption to a routinely-rotated key, and never let a failed-decrypt sentinel value be written back over the original ciphertext (no error-swallow + full overwrite-save).
Issue: _get_fernet() derives a single Fernet key (no MultiFernet) from ENCRYPTION_KEY-or-SECRET_KEY, and from_db_value swallows decrypt failures returning '' — so rotating SECRET_KEY (when no ENCRYPTION_KEY is set) renders all stored credentials undecryptable, and the next full save() re-encrypts the '' and permanently destroys the original ciphertext.
Fix: Support a key list via MultiFernet (try each key on decrypt, encrypt with the primary) to make rotation non-destructive; and have from_db_value re-raise / return a distinct sentinel rather than '' on decrypt failure so a corrupted read can never be silently re-saved as empty (or guard SystemSettings.save() to skip encrypted fields whose in-memory value is '' but whose stored ciphertext is non-empty). The existing checks.py W001 warning is good but advisory only.
```

### M8. apps/integrations/views.py
```
[SEVERITY: MEDIUM]
File: apps/integrations/views.py
Line: 902-1143 (the post() method; the class begins at 873)
Principle: THIN VIEWS / FAT SERVICES (1)
Issue: ZendeskClaimWebhookView.post is a ~240-line method orchestrating secret verification, ticket fetch, claim-number gating, LLM extraction, email-resolution fallbacks, status resolution, and Claim creation — all business logic embedded in the view.
Fix: Extract the creation path (everything after the `if claim:` dispatch at line 937, i.e. lines 939-1136) into a service function in apps/integrations/services.py, e.g. create_claim_from_zendesk_ticket(ticket_id, payload) that returns a small result object/dict (created | already_exists | ignored | fetch_failed) the view maps to a Response. The view would then only: verify the secret, parse ticket_id/custom_status, dispatch to either _handle_status_change or the service, and translate the result to status codes. _handle_status_change (1145-1235) is a second, smaller candidate for extraction but is lower priority. Pure refactor — preserve the existing hmac secret check, the DB-unique/IntegrityError idempotency guard, and the best-effort AI-summary semantics exactly.
```

### M9. apps/claims/views.py
```
[SEVERITY: MEDIUM]
File: apps/claims/views.py
Line: 239-294 (the post() method body; class spans 217-294)
Principle: Thin views / fat services (1)
Issue: ClaimUpdateFromZendeskView.post contains the full re-extraction business logic (OVERWRITE/FILL field loops, date/price coercion, deadline recompute, conditional save, timeline write) inline in the view instead of a service function.
Fix: Extract the merge logic into apps/claims/services.py as `refresh_claim_from_zendesk(claim, ticket_data, extracted) -> list[str]` (or have it call analyze_zendesk_ticket_for_claim internally), returning the list of updated_fields and performing the conditional save(update_fields=...). The view then: fetches the ticket/comments, calls the service, calls refresh_claim_summary, writes the timeline row, and builds the Response — staying thin. This matches the existing services.py convention and makes the overwrite-vs-fill-only and date/price coercion rules directly unit-testable without HTTP.
```

### M10. requirements.txt / .venv
```
[SEVERITY: MEDIUM]
File: requirements.txt / .venv
Line: requirements.txt: Django==5.2.14
Principle: Django best-practices (7) — dev/prod environment parity
Issue: The project venv resolves Django 4.2.29 while requirements.txt pins Django==5.2.14 (Railway builds from requirements), so local development and the test suite run against a different major Django version than production.
Fix: Recreate the local venv from requirements.txt (pip install -r requirements.txt) so dev/CI run Django 5.2.14, matching prod; pin Django in CI and add a smoke check that asserts django.get_version() matches the pin.
```

---

## Evaluated and dismissed (adversarially refuted)

_These were flagged by a reviewer or the completeness critic, then refuted by reading the real code. Recorded so the reasoning is on file._

- **[HIGH → dismissed]** apps/payments/paypal_disputes_service.py — `accept_claim()` double-refund via `@retry`  
  The `@retry` decorator is inert: the function's inner `except HTTPError/URLError` blocks catch and `return False` for the exact exceptions tenacity retries on, so it never re-fires. Exactly one POST per call. The caller also guards with a terminal-status check + atomic in-flight CAS + post-call re-sync. (Worth a LOW: remove the dead decorator.)
- **[MEDIUM → dismissed]** apps/payments/paypal_disputes_service.py — `provide_evidence()` duplicate evidence POST  
  Same as above — exceptions are swallowed, decorator never retries. No duplicate submission.
- **[MEDIUM → dismissed]** apps/payments/paypal_disputes_service.py — `document_ids` won't bind to multipart parts  
  Refuted: PayPal binds `evidences[].document_ids` to the multipart part *name*; the code sets part name = filename = the same value used in `document_ids`, so they match by construction (and a test asserts it).
- **[MEDIUM → dismissed]** apps/payments/models.py — `mark_failed` JSONField lost-update  
  Refuted for this codebase: every call site freshly creates or freshly loads the row immediately before mutate+save in a single linear request flow; no concurrent writer exists (no Celery/Redis).
- **[MEDIUM → dismissed]** apps/integrations/services.py:959 — alias injected into Zendesk search query  
  Refuted: `alias` is produced only by a regex (`[\w.-]+@[\w.-]+\.\w+`) that cannot emit quotes/operators, and the query is URL-encoded on the wire. No breakout possible.
- **[HIGH → dismissed]** apps/config/models.py:45 — `auto_now_add` freezes `last_checked`  
  Refuted (the claim describes `auto_now`, not `auto_now_add`): `auto_now_add` only fires on INSERT, so the manual `last_checked = now()` assignments on update *are* persisted. Verified against Django source + existing tests.
- **[MEDIUM → dismissed]** apps/core/.../run_scheduled_jobs.py — no dispatcher run-lock  
  Refuted as harmful: there's no lock, but every job is independently overlap-safe (client-updates use an atomic per-row CAS; email sweep dedups on a unique Message-ID). Overlap wastes work but causes no double-send/data-loss.
- **[MEDIUM → dismissed]** apps/payments/woocommerce_service.py — duplicate POST → second real refund  
  Refuted: the sole caller reserves+caps the amount under `select_for_update` and creates a PENDING row *before* the call, and explicitly honors the `indeterminate` flag (no auto-retry). A duplicate same-amount POST is rejected by the cap.
- **[MEDIUM → dismissed]** apps/integrations/views.py — Zendesk webhook redelivery duplicates a Claim  
  Refuted: `zd_ticket_id` is `unique=True`; create is wrapped in `atomic` with an `IntegrityError` re-query, and redelivery hits the early existence check + same-status no-op (which returns before the timeline write). Fully idempotent.

---

## LOW-severity findings (135)

_Polish / maintainability / defense-in-depth. None are bugs or live vulnerabilities. Grouped by app._

### payments (44)

- **`apps/payments/document_service.py:1729-1758 (regenerate_document); the hardcoded version=1 / _v1_ filename / "(v1)" logs are at lines 581, 589, 595 in generate_response_letter and 1704, 1712, 1718 in generate_evidence_report`** — _Atomicity & Data Integrity (9)_  
  regenerate_document creates a fresh document via generate_* (which hardcodes version=1, writes a filename containing _v1_, and logs a "(v1)" DOCUMENT_GENERATED entry) and only afterward bumps the version field, leaving the saved PDF filename and one activity-log line permanently saying v1 for what is actually v2+.  
  **Fix:** Compute the next version from existing DisputeDocument rows (or accept a version argument) inside generate_response_letter/generate_evidence_report so the filename (_vN_), the version field, the content, and a single DOCUMENT_GENERATED log entry are all written consistently in the one narrow transaction. Then have regenerate_document simply pass the target version (old_document.version + 1) and emit no second DOCUMENT_GENERATED log, eliminating both the stale _v1_ filename and the duplicate log line.
- **`apps/payments/document_service.py:522`** — _No Magic Numbers/Strings (3)_  
  The Zendesk alias custom-field ID is hardcoded as the bare literal 13606076120860, even though it is already defined as the named constant ZENDESK_FIELD_ALIAS_EMAIL in apps/integrations/services.py:49, so the two will silently diverge if the field ID ever changes.  
  **Fix:** Replace the inline lookup with the existing helper: in document_service.py, import get_ticket_email_alias from apps.integrations.services and set `alias = get_ticket_email_alias(ticket)`, deleting the manual for-loop at lines 520-524. This reuses the single source of truth (and also gains the helper's lowercase normalization). Separately, consider collapsing the two duplicate definitions in services.py (ZENDESK_FIELD_ALIAS_EMAIL int at :49 and EMAIL_ALIAS_FIELD_ID str at :901) into one to eliminate the remaining divergence risk.
- **`apps/payments/document_service.py:1278-1283, 958-978`** — _Slow Queries / N+1 (6)_  
  Within one bundle build the EmailLog table is queried twice for the same claim: once in _fetch_communication_history (filter(claim=...)[:50]) and again in _identity_context (filter(claim=...).order_by('received_at')), duplicating I/O for data that could be fetched once.  
  **Fix:** Fetch the claim's EmailLog rows once in build_dispute_evidence_bundle and pass the list into both _fetch_communication_history and _identity_context.
- **`apps/payments/document_service.py:473, 1642, 1267`** — _Testability & Code Quality (10)_  
  generate_response_letter, generate_evidence_report, and build_dispute_evidence_bundle are large multi-responsibility functions (fetch + AI + render + persist + log) well over ~40 lines, making them hard to unit test in isolation.  
  **Fix:** Extract the persist+log step (create DisputeDocument, save file, write activity log) into a shared helper so both generators reuse it and the orchestration body shrinks; this also removes the duplicated transaction.atomic block.
- **`apps/payments/document_service.py:582-596, 1705-1719`** — _DRY (2)_  
  The narrow-transaction block that creates a DisputeDocument, saves the PDF ContentFile, and writes a DOCUMENT_GENERATED DisputeActivityLog is copy-pasted almost verbatim in both generate_response_letter and generate_evidence_report.  
  **Fix:** Factor the create+file-save+log into one helper (e.g. _persist_document(dispute, doc_type, generated_by, content_html, pdf_bytes, version, details)) and call it from both.
- **`apps/payments/document_service.py:473, 1642`** — _Idempotency (5)_  
  Both generators always create a brand-new DisputeDocument with version=1 and no uniqueness guard on (dispute, doc_type, version), so a double click / retry of the generate button produces duplicate v1 documents.  
  **Fix:** Either add a unique_together on (dispute, doc_type, version) and compute the next version, or guard against an existing in-flight DRAFT document for the same dispute/doc_type before creating.
- **`apps/payments/document_service.py:566, 581, 1338, 1704`** — _Django Best Practices (7)_  
  Naive datetime.now() (no tzinfo) is used for the generated_at display value and filename timestamps, inconsistent with Django's USE_TZ-aware datetimes used elsewhere (e.g. received_at, created_at) and can render misleading times.  
  **Fix:** Use django.utils.timezone.now() / localtime() for the displayed timestamp; for filenames the naive call is harmless but timezone.now() keeps it consistent.
- **`apps/payments/document_service.py:48-64`** — _Testability & Code Quality (10)_  
  _call_qwen_ai has typed keyword params but no declared return type annotation despite returning a (subject, body) tuple that callers depend on.  
  **Fix:** Add a return annotation, e.g. -> Tuple[str, str], to make the contract explicit.
- **`apps/payments/document_service.py:127-134, 667-669`** — _Security (8)_  
  MIME type for embedded images is derived purely from the (attacker-influenceable, though staff-uploaded) filename extension rather than the actual file bytes, so a mislabeled file embeds with a wrong/spoofed data-URI MIME; ALLOWED_HTML_ATTRIBUTES is also defined but never passed to bleach in this module.  
  **Fix:** Sniff the real format with Pillow (already imported in _downscale_for_embed) for the MIME, and confirm ALLOWED_HTML_TAGS/ALLOWED_HTML_ATTRIBUTES are actually wired into a bleach.clean call (they appear unused here).
- **`apps/payments/frontend_views.py:718-724 (provide_evidence call in dispute_send_evidence), contrasted with the mutex in dispute_accept_claim at 956-959 / released at 1005`** — _Atomicity & data integrity / Idempotency (9,5)_  
  The legacy send-evidence view (dispute_send_evidence) calls provide_evidence() WITHOUT the outbound_in_flight compare-and-set mutex that dispute_accept_claim uses, so two concurrent submit clicks can both reach PayPal and submit evidence twice.  
  **Fix:** Cheapest correct fix: delete the dead dispute_send_evidence view and its route in apps/payments/frontend_urls.py (and the test in test_dispute_phase4.py), since no template links it and the live submit flow goes through dispute_submit_to_paypal/dispute_manual_reply. If it must be kept as a fallback, mirror dispute_accept_claim: wrap provide_evidence in an atomic compare-and-set guard (Dispute.objects.filter(pk=dispute.pk, outbound_in_flight=False).update(outbound_in_flight=True); bail if zero rows updated) and release it in a finally block.
- **`apps/payments/paypal_disputes_service.py:117-194`** — _Idempotency / correctness under retry (5)_  
  get_paypal_access_token() is @retry-decorated but its body catches HTTPError/URLError and returns None instead of re-raising, so tenacity never sees the exception and the retry decorator can never actually retry — the retry is dead/ineffective.  
  **Fix:** Let the network exceptions propagate out of the function so @retry can act, and convert to None only after retries are exhausted (or drop the @retry decorator since it does nothing here).
- **`apps/payments/paypal_disputes_service.py:81`** — _Testability & code quality (10)_  
  import uuid is done inside _encode_multipart() at call time rather than at module top, an inconsistent local import that hides a dependency and is needlessly re-imported on every call.  
  **Fix:** Move 'import uuid' to the module-level import block alongside base64/json/socket.
- **`apps/payments/paypal_disputes_service.py:682,691,728,751,775`** — _Testability & code quality (10)_  
  Several public service functions lack return-type hints and/or use a mutable-ish default of None for a list arg without annotation (provide_supporting_info, provide_evidence_files, _build_submission_files return, submit_dispute_response's submission param is untyped).  
  **Fix:** Add explicit return type hints (e.g. -> Tuple[bool, Optional[dict]]) and annotate files: Optional[List[dict]] = None; type the submission param to the DisputeSubmission model for clarity and tooling.
- **`apps/payments/paypal_disputes_service.py:880-948`** — _Slow queries / N+1 (6)_  
  _match_claim_for_dispute runs up to four separate Claim.objects.filter(...).first() queries per dispute; on a backfill loop over many disputes (list_paypal_disputes + ingest_dispute) this is several queries per dispute with no batching.  
  **Fix:** Acceptable for the low-volume backfill path, but if backfilling large batches consider collecting identifiers first and resolving claims in fewer queries; otherwise leave as-is and note it is bounded by dispute count.
- **`apps/payments/paypal_disputes_service.py:86-89`** — _Django best practices / correctness (7)_  
  The multipart JSON 'input' part declares Content-Type: application/json with no charset, while the JSON is encoded UTF-8; some strict multipart parsers may misread non-ASCII notes (e.g. accented buyer names) in the evidence payload.  
  **Fix:** Emit 'Content-Type: application/json; charset=utf-8' for the input part to match the utf-8 encoding used on line 89.
- **`apps/payments/refund_service.py:515 (the create call spans 512-525; the offending line is amount=refund_amount at line 515)`** — _DJANGO BEST PRACTICES (7)_  
  In process_woocommerce_refund the amount stored is the raw refund_amount from the webhook payload (a str off request.data), while every comparison nearby uses Decimal(str(refund_amount)); persisting an unconverted/unvalidated value risks storing a string or malformed decimal and diverges from the validated comparison path.  
  **Fix:** Coerce and validate once near the top of process_woocommerce_refund, before any comparison or persistence: amount = Decimal(str(refund_amount)) wrapped in try/except (InvalidOperation, TypeError) that returns {'success': False, 'error': 'Invalid refund amount'} on failure. Then use that single amount variable for the reservation match (currently line 485), the price_paid comparison (line 507), and the Refund.objects.create(amount=...) (line 515). This removes the divergence and turns malformed input into a clean validated error instead of an opaque caught exception. Optionally also coerce at the webhook boundary in apps/integrations/views.py before calling the service.
- **`apps/payments/refund_service.py:261-264, 290, 314, 484, 514`** — _NO MAGIC STRINGS (3)_  
  Refund status strings ('PENDING','PROCESSING','COMPLETED','FAILED'), source strings ('LORA','WOOCOMMERCE'), type strings ('FULL','PARTIAL') and the 'WC-' / 'WC-PENDING-' id prefixes are repeated as bare literals across both files and the views instead of referencing the model's STATUS_CHOICES/SOURCE_CHOICES or named constants.  
  **Fix:** Expose constants on the Refund model (e.g. Refund.STATUS_PENDING, Refund.SOURCE_WOOCOMMERCE, WC_PREFIX='WC-') and reference them everywhere; only RESERVING_STATUSES is currently named.
- **`apps/payments/refund_service.py:140-145, 220-225, 305-310, 536-541, 581-583`** — _TESTABILITY & CODE QUALITY (10)_  
  Several methods wrap their entire body in a bare 'except Exception' that returns/logs a generic error, swallowing programming errors (e.g. AttributeError, KeyError) and making real bugs indistinguishable from expected failures.  
  **Fix:** Catch the specific expected exceptions (network/HTTP, Decimal InvalidOperation, IntegrityError) and let unexpected ones propagate, or at minimum log+re-raise unexpected types rather than returning success/None silently.
- **`apps/payments/refund_service.py:281-283, 91-93`** — _DJANGO BEST PRACTICES (7)_  
  process_webhook_refund derives capture_id from a guessed payload path ('seller_payable_breakdown'->'payable_version'->'id') marked 'May need adjustment', and currency is hardcoded 'USD' with a TODO; this path can silently create COMPLETED refunds with a blank capture_id / wrong currency.  
  **Fix:** Resolve capture_id and currency from the documented PayPal PAYMENT.CAPTURE.REFUNDED payload (links/up reference and amount.currency_code) and validate them before creating a COMPLETED row, or mark the row needing reconciliation when they are absent.
- **`apps/payments/refund_service.py:316-411`** — _TESTABILITY & CODE QUALITY (10)_  
  issue_woocommerce_refund is ~95 lines doing validation, atomic reservation, external HTTP, three distinct failure branches, and success reconciliation, and uses function-local imports (uuid, Sum, timezone) inside the method.  
  **Fix:** Move imports to module top (uuid is already imported at module level — the local 'import uuid' shadows it) and extract the reservation step and the success-stamp step into small private helpers to shorten the function and ease unit testing.
- **`apps/payments/refund_service.py:413-428`** — _SLOW QUERIES / N+1 (6)_  
  _find_claim_for_refund filters Claim by alf_claim_id__iexact and then by id without select_related on commonly-accessed related fields; the caller (RefundWebhookView) immediately accesses result.refund.claim.zd_ticket_id, but the lookup itself is fine — the concern is the case-insensitive iexact on alf_claim_id, which cannot use a standard b-tree index efficiently.  
  **Fix:** If alf_claim_id lookups are hot, store/normalize it consistently (e.g. uppercase on save) and use an exact indexed lookup, or add a functional index; confirm alf_claim_id is db_indexed.
- **`apps/payments/views.py:29`** — _DRY / SEPARATION OF CONCERNS (4)_  
  views.py still imports IsManager, IsAgentOrManager from apps.users.permissions even though the manager/agent role split was deliberately removed (single trusted user type), leaving stale permission machinery referenced from the refund views.  
  **Fix:** Confirm these permissions are no longer applied and remove the import (and the permission classes) in favor of IsAuthenticated, per the single-user-type decision.
- **`apps/payments/frontend_views.py:154-181`** — _Atomicity & Data Integrity (9)_  
  dispute_create writes two rows across models (Dispute.objects.create then DisputeActivityLog.objects.create) without transaction.atomic(), so a failure between them leaves a dispute with no creation log entry.  
  **Fix:** Import `transaction` from django.db and wrap the Dispute.objects.create plus DisputeActivityLog.objects.create in a single `with transaction.atomic():` block, keeping the existing IntegrityError handling around (or just inside) it so the duplicate-ppid message still works. This guarantees the audit log is created with the dispute or neither persists.
- **`apps/payments/frontend_views.py:410-414`** — _Atomicity & Data Integrity (9)_  
  dispute_link_claim does dispute.save(update_fields=...) and then DisputeActivityLog.objects.create() as two separate writes with no transaction.atomic(), risking a linked dispute with no audit log on partial failure.  
  **Fix:** Add `from django.db import transaction` to the imports and wrap the two writes: `with transaction.atomic(): dispute.save(update_fields=update_fields); DisputeActivityLog.objects.create(...)`. This guarantees the link/MATCHED state and its audit-log row commit together or roll back together. Low priority given the audit-only nature of the at-risk record.
- **`apps/payments/frontend_views.py:668-672`** — _Atomicity & Data Integrity (9)_  
  dispute_set_category persists the new category and then creates the activity log in two un-grouped writes; a crash in between yields a category change with no audit trail.  
  **Fix:** Wrap both writes in a single with transaction.atomic(): block (add from django.db import transaction) so the category change and its DisputeActivityLog entry commit together or not at all. Apply the same grouping to the sibling save+log handlers (lines 410-411, 615-618, 642-649) for consistency, since they share the identical pattern.
- **`apps/payments/frontend_views.py:612-622`** — _Atomicity & Data Integrity (9)_  
  dispute_accept_document saves the document (status/accepted_at/accepted_by) and then writes the DOCUMENT_ACCEPTED log as two separate, non-atomic operations.  
  **Fix:** Optionally wrap the document.save() and DisputeActivityLog.objects.create() in a single with transaction.atomic(): block so the status change and its audit-log entry commit together. Low priority and best applied consistently across the other save-then-log views in this file rather than only here.
- **`apps/payments/views.py:211-236`** — _Thin Views / Fat Services (1)_  
  RefundViewSet.create builds the Refund inline in the view — generating the paypal_refund_id, hardcoding status='COMPLETED' and external_source='MANUAL' — instead of delegating to a RefundService method like the other actions do.  
  **Fix:** Add RefundService.create_manual_refund(claim, amount, currency, refund_type, reason, user) that encapsulates the synthetic paypal_refund_id generation, status='COMPLETED', and external_source='MANUAL', then have RefundViewSet.create call it — matching how process/issue delegate. Keeps all Refund-construction logic in the one service layer. Low priority, no behavior change.
- **`apps/payments/frontend_views.py:133/160/179, 407-408, 612, 620, 790, 839-840, 851, 900, 925, 947, 958-959 (and DisputeActivityLog action literals at 179, 412, 620, 644, 671)`** — _No Magic Numbers/Strings (3)_  
  Dispute/submission/document status and action strings ('MATCHED','RECEIVED','DRAFT','SUBMITTING','ACCEPTED','RESOLVED_WON','STATUS_CHANGED','DISPUTE_MATCHED','DOCUMENT_ACCEPTED', etc.) are hardcoded as bare literals across many views rather than referenced from model choice constants.  
  **Fix:** Convert the choice lists on Dispute, DisputeDocument, DisputeActivityLog, and DisputeSubmission to django.db.models.TextChoices (e.g. class Status(models.TextChoices): MATCHED = 'MATCHED', 'Matched to Zendesk Ticket'; class Action(models.TextChoices): STATUS_CHANGED = 'STATUS_CHANGED', 'Status Changed'), keep the existing wire values identical so no data migration is needed (PayPal reason enums must stay verbatim), then replace the bare literals in frontend_views.py with Dispute.Status.MATCHED, DisputeSubmission.Status.DRAFT/SUBMITTING, DisputeActivityLog.Action.STATUS_CHANGED, etc. This is a low-priority maintainability cleanup, not urgent for a trusted-staff internal tool.
- **`apps/payments/frontend_views.py:947, 989`** — _DRY (2)_  
  The terminal-status tuple ('RESOLVED_WON','RESOLVED_LOST','ACCEPTED') is re-typed inline in dispute_accept_claim even though the model already exposes Dispute.TERMINAL_STATUSES (used elsewhere in the same file).  
  **Fix:** Replace both inline tuples with `dispute.status in Dispute.TERMINAL_STATUSES`.
- **`apps/payments/frontend_views.py:133 vs 665`** — _DRY (2)_  
  Reason validation is done two different ways — dispute_create checks `reason in Dispute.VALID_REASONS` while dispute_set_category checks `category not in dict(Dispute.REASON_CHOICES)`, rebuilding the dict each call.  
  **Fix:** Use the cached Dispute.VALID_REASONS dict in both places (drop the per-request dict(REASON_CHOICES) build).
- **`apps/payments/serializers.py:85-89`** — _Django Best Practices (7)_  
  validate_amount only enforces amount > 0 with no upper bound, so a manual refund/process can be created for an arbitrarily large value with no sanity ceiling against the claim's price_paid.  
  **Fix:** Add an upper-bound check (e.g. against claim.price_paid or a configured max) in the serializer or service to prevent fat-finger over-refunds.
- **`apps/payments/views.py:211-236`** — _Idempotency (5)_  
  RefundViewSet.create has no dedup guard — repeated POSTs each insert a new MANUAL refund row with a fresh random paypal_refund_id, so a double-submit silently creates duplicate money records.  
  **Fix:** Accept an idempotency key (or dedupe on claim+amount+reason within a short window) before creating the manual Refund, mirroring the placeholder-id pattern used in initiate_refund.
- **`apps/payments/frontend_views.py:1, 2`** — _Testability & Code Quality (10)_  
  Module and view docstrings still say 'MANAGER role only' / '(MANAGER role only)' even though the manager/agent role split was removed and @manager_required is now just a login gate — stale/misleading documentation.  
  **Fix:** Update the docstrings to say 'authenticated staff only' to match the current single-user-type auth model.
- **`apps/payments/views.py:159-176`** — _Django Best Practices (7)_  
  RefundViewSet docstrings and the get_permissions comment describe AGENT vs MANAGER role behavior, but IsManager/IsAgentOrManager are now both aliases of IsAuthenticated, so the documented role distinction no longer exists.  
  **Fix:** Update the docstrings/comments to reflect that all actions require only authentication; optionally drop the now-no-op get_permissions override.
- **`apps/payments/views.py:219, 355`** — _Testability & Code Quality (10)_  
  Imports are done inside method bodies (import uuid in create; from django.db.models import Sum, Count, Q inside stats) rather than at module top, hiding dependencies and re-importing on every call.  
  **Fix:** Move `import uuid` and the django.db.models imports to the module-level import block (Sum/Q are already imported at top of the file).
- **`apps/payments/models.py:131-147`** — _Atomicity & Data Integrity (9)_  
  Refund.mark_completed/mark_failed/mark_processing each call self.save() with no update_fields, so the full row is rewritten and a concurrent status change (e.g. PayPal webhook vs. admin action) silently clobbers the other's fields.  
  **Fix:** If hardening is desired, wrap the status transitions in transaction.atomic() and re-load the row with select_for_update() before mutating, so a concurrent webhook and manual update serialize rather than race. If only narrowing the write is wanted, pass update_fields matching exactly what each method mutates — mark_completed: ['status','processed_at','updated_at']; mark_failed: ['status','metadata','updated_at']; mark_processing: ['status','updated_at'] — but note this alone does NOT prevent the status clobber the finding describes, since both writers set status. Given the low volume and single-staff trust model, this is optional cleanup, not a required fix.
- **`apps/payments/models.py:661-706`** — _Testability & Code Quality (10)_  
  ProcessedWebhookEvent.is_already_processed, mark_as_processed, and mark_as_failed are dead code: the live PayPal webhook in views.py does idempotency inline via ProcessedWebhookEvent.objects.get_or_create, and these classmethods are referenced only from tests.  
  **Fix:** Either route the webhook view through these classmethods (so idempotency logic has one home) or delete the unused classmethods to avoid drift between two competing idempotency implementations.
- **`apps/payments/models.py:133,139,145,151,154`** — _No Magic Numbers/Strings (3)_  
  Status string literals ('COMPLETED','FAILED','PROCESSING','PENDING') are hardcoded inside the mark_* methods and is_completed/is_pending properties instead of referencing named constants, so a change to STATUS_CHOICES won't be caught by these call sites.  
  **Fix:** Define module/class constants (e.g. STATUS_COMPLETED = 'COMPLETED', ...) used both in STATUS_CHOICES and these methods, mirroring how Dispute.TERMINAL_STATUSES centralizes its status groupings.
- **`apps/payments/admin.py:132-145`** — _Separation of Concerns / Layering (4)_  
  The admin bulk actions mark_completed/mark_failed/mark_cancelled write refund status with a raw queryset.update(), bypassing the Refund.mark_* model methods and any side effects (processed_at is set only for completed, never for failed/cancelled; no audit/metadata).  
  **Fix:** Have the admin actions iterate and call the model's mark_* methods (or move the shared transition logic into a service/model method the admin reuses) so status transitions are consistent regardless of entry point.
- **`apps/payments/utils.py:55-92`** — _Slow Queries / N+1 (6)_  
  generate_proof_of_work_pdf opens and reads each evidence image inside a loop over claim.evidence.all() with no select_related/iterator and reads whole files into memory base64-encoded; for a claim with many large images this loads everything into RAM at once.  
  **Fix:** Stream/iterate evidence (.iterator()) and consider capping image count/size; if related fields are accessed add select_related, and ensure the file handle opened via evidence.image.open('rb') is closed (currently never .close()d).
- **`apps/payments/utils.py:60-67`** — _Security (8)_  
  The path-traversal guard uses abs_path.startswith(media_root) without a separator, so a sibling directory like '/srv/media_evil' would pass when MEDIA_ROOT is '/srv/media'; also evidence.image.path raises on non-filesystem (e.g. S3) storage backends.  
  **Fix:** Compare with os.path.commonpath([abs_path, media_root]) == media_root (or append os.sep before startswith), and read via evidence.image.open() on the storage API rather than .path so it works with remote storage.
- **`apps/payments/utils.py:200-210`** — _Testability & Code Quality (10)_  
  generate_proof_of_work_pdf is a ~180-line function doing fetching, file IO, encoding, templating, inline CSS, and PDF rendering with broad try/except that swallows all errors into a None return, making failures hard to distinguish (WeasyPrint missing vs. bad data vs. Zendesk down all return None).  
  **Fix:** Split into helpers (gather evidence, fetch comments, render+convert) and either raise typed exceptions or return a result object so callers can tell why generation failed; move the large CSS block to a template/static file.
- **`apps/payments/models.py:300-309`** — _Separation of Concerns / Layering (4)_  
  deadline_state imports django.utils.timezone inside the method body rather than using the module-level import already present at the top of the file (line 3), an inconsistent local import with no lazy-loading justification.  
  **Fix:** Remove the inline 'from django.utils import timezone' in deadline_state and rely on the existing module-level import.
- **`apps/payments/paypal_disputes_service.py:963-1028 (check-then-create); caller apps/payments/frontend_views.py:306-312`** — _Idempotency / TOCTOU on a unique-constrained insert [completeness pass]_  
  ingest_dispute does a check-then-create on the unique paypal_dispute_id with no atomic block or IntegrityError guard, so two concurrent ingests of the same dispute can race the unique constraint and make the second create() raise IntegrityError.  
  **Fix:** Replace the .first()/.create() pattern with Dispute.objects.get_or_create(paypal_dispute_id=dispute_id, defaults={...}) so the insert is atomic and a duplicate returns (existing, False) idempotently. Keep the self-heal/re-match logic on the not-created branch. Optionally wrap in transaction.atomic to keep the activity-log writes consistent.

### integrations (19)

- **`apps/integrations/views.py:156-174 (GET), 376-385, 523-532, 601-610, 1257-1266, 1503-1512, 1574-1583, 1619-1628 (POST); note 711-715 is a shorter inconsistent variant (bare 403, no rate-limit block)`** — _DRY (2)_  
  The exact same Zendesk sidebar auth-fail handling block (authenticate -> read REMOTE_ADDR -> increment cache key -> 429-after-5 / 403) is copy-pasted verbatim across at least 7 POST views and one GET view.  
  **Fix:** Extract a DRF permission class (e.g. IsSidebarAuthenticated/SidebarTokenPermission) that wraps ZendeskSidebarAuth.authenticate plus the IP-keyed cache rate-limiting, and apply it via permission_classes on each sidebar view, replacing AllowAny. DRF's throttling/permission machinery (or a custom permission raising Throttled/PermissionDenied) lets each view body drop the duplicated block. This also fixes the inconsistency at 711-715, which currently lacks the rate-limit protection the other endpoints have.
- **`apps/integrations/views.py:1322-1337`** — _ATOMICITY & DATA INTEGRITY (9)_  
  In the flight-lookup success path a claim.save() and a separate ClaimUpdateTimeline.objects.create() (two cross-model writes) run without transaction.atomic(), so a crash between them leaves claim.flight_data persisted with no matching timeline entry.  
  **Fix:** Reorder so the two DB writes are adjacent and wrap only them in transaction.atomic(), keeping the external Zendesk _post_note() call OUTSIDE the transaction. e.g. compute note_posted first (or after), then do: with transaction.atomic(): claim.flight_data=...; claim.save(update_fields=[...]); ClaimUpdateTimeline.objects.create(...). Do NOT enclose self._post_note() inside the atomic block (it is an HTTP call). Apply the same adjacent-writes-only pattern in _handle_no_number and _handle_not_found. Given the low impact (cache + audit log), this is optional cleanup rather than urgent.
- **`apps/integrations/views.py:331, 337`** — _NO MAGIC NUMBERS/STRINGS (3)_  
  Email category strings 'SUBMISSION_CONFIRMATION' and 'GENERAL_CORRESPONDENCE' are hardcoded as bare literals in the filter, duplicating values that are already defined choices on EmailLog.CATEGORY_CHOICES (apps/communications/models.py:13-18).  
  **Fix:** Define named constants/TextChoices on the EmailLog model (e.g. EmailLog.Category.SUBMISSION_CONFIRMATION) and reference them here instead of raw strings, so a rename can't silently break this count.
- **`apps/integrations/views.py:898-899`** — _NO MAGIC NUMBERS/STRINGS (3)_  
  The Zendesk 'Investigation Initiated' custom-status ID is a bare hardcoded string literal on the class, an environment/tenant-specific identifier living in code.  
  **Fix:** Source INVESTIGATION_STATUS_ID from SystemSettings (alongside the other trigger-status IDs already stored there, e.g. client_report_trigger_status_id) or from settings/env, rather than hardcoding it in the view class.
- **`apps/integrations/views.py:158, 377, 524, 602, 1258, 1504, 1575, 1621`** — _SECURITY (8)_  
  The auth-fail rate limiter keys on request.META['REMOTE_ADDR']; behind Railway's proxy this is the proxy's IP, so all callers share one bucket and the per-IP brute-force throttle is effectively global (and trivially evaded if a forwarded header were trusted elsewhere).  
  **Fix:** Derive the client IP from the proxy-forwarded header (validated, e.g. X-Forwarded-For left-most after configuring trusted proxies) or rely on DRF's configured throttle scope instead of REMOTE_ADDR for the brute-force key.
- **`apps/integrations/views.py:256-289`** — _SLOW QUERIES / N+1 (6)_  
  _get_emails_data issues four separate queries against EmailLog for the same claim (total count, unresolved count, latest, category breakdown) when total and the per-category breakdown could be derived from one aggregation pass.  
  **Fix:** Combine the counts using a single .aggregate() with conditional Count/Q (e.g. total=Count('id'), unresolved=Count('id', filter=Q(action_required=True, auto_resolved=False))) and reuse the category_breakdown query, reducing round-trips per sidebar open.
- **`apps/integrations/views.py:744-748`** — _THIN VIEWS / FAT SERVICES (1)_  
  ZendeskTicketSyncView.post builds the Zendesk subject/comment body and tag list inline in the view (presentation/business logic) before calling the service.  
  **Fix:** Move the subject/comment/tags composition into create_zendesk_ticket or a dedicated service helper that accepts the claim, keeping the view to dispatch-and-respond.
- **`apps/integrations/services.py:services.py:49 and services.py:901; third literal at apps/payments/document_service.py:522`** — _DRY / No Magic Numbers (2, 3)_  
  The same Zendesk alias custom-field ID is declared as two separate constants with different types (int `ZENDESK_FIELD_ALIAS_EMAIL` and str `EMAIL_ALIAS_FIELD_ID`) and is also hard-coded a third time in apps/payments/document_service.py:522, so they can silently diverge.  
  **Fix:** Define a single canonical constant in apps/integrations/services.py — keep `ZENDESK_FIELD_ALIAS_EMAIL: int = 13606076120860` as the source of truth. Replace the duplicate `EMAIL_ALIAS_FIELD_ID = '13606076120860'` (line 901) with `EMAIL_ALIAS_FIELD_ID = str(ZENDESK_FIELD_ALIAS_EMAIL)` (or use the int directly and cast inline where the search-query/dict-id-string comparison needs a string at lines 910 and 959). In apps/payments/document_service.py, import the canonical constant and compare via `str(cf.get('id')) == str(ZENDESK_FIELD_ALIAS_EMAIL)` (or import the helper get_ticket_email_alias from services.py and reuse it) instead of repeating the raw literal at line 522. Test fixtures may keep the literal, but consider importing the constant there too.
- **`apps/integrations/services.py:847-857`** — _Testability & Code Quality (10)_  
  `_pick_best_result` declares a `transaction_date` parameter that is never used in its body (it only sorts by created_at desc), and the parameter is passed inconsistently at the call sites — dead code that misleads readers into thinking date-proximity matching happens.  
  **Fix:** Either drop the unused `transaction_date` parameter (and the values passed in at lines 863/881) or implement the date-proximity ranking the parameter implies.
- **`apps/integrations/services.py:936`** — _No Magic Numbers (3) / DRY (2)_  
  `add_zendesk_ticket_tags` uses a hard-coded `timeout=30` while every other Zendesk call in this module reads the configurable `getattr(settings, 'ZENDESK_TIMEOUT', 30)`, so a deployment that raises/lowers ZENDESK_TIMEOUT silently won't affect tag updates.  
  **Fix:** Use `timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)` here too for consistency with the rest of the module.
- **`apps/integrations/services.py:1068-1223`** — _Testability & Code Quality (10)_  
  `analyze_zendesk_ticket_for_claim` is ~155 lines doing field reading, LLM prompt assembly, the LLM call, merging, and logging in one function, which makes it hard to unit test the pieces independently.  
  **Fix:** Extract the structured-field read (Step 1) and the LLM context/prompt build (Step 2) into small helpers (e.g. `_read_structured_fields`, `_build_llm_context`) so the merge logic can be tested without mocking the whole flow.
- **`apps/integrations/services.py:904-913`** — _DRY / Separation of Concerns (2, 4)_  
  `get_ticket_email_alias` re-implements custom-field lookup by id (with `str(field.get('id'))`) instead of reusing the existing `_get_custom_field_value` helper, and document_service.py:522 does the same lookup a third way, giving three subtly different field-reading code paths.  
  **Fix:** Have `get_ticket_email_alias` call `_get_custom_field_value(ticket_data.get('custom_fields'), ZENDESK_FIELD_ALIAS_EMAIL)` and lowercase/strip the result, and route document_service.py through the same helper.
- **`apps/integrations/services.py:1136`** — _Django Best Practices / Separation of Concerns (7, 4)_  
  When building LLM context, comment author is read via `comment.get('author', {}).get('name', ...)` but ticket comments fetched by this module nest the name under `author` already; if a caller passes the legacy/string-comment shape used by build_ticket_thread this silently yields 'Unknown', masking malformed input rather than normalizing it.  
  **Fix:** Reuse a single comment-normalization path (or assert the expected dict shape) so author resolution is consistent between fetch_zendesk_comments, build_ticket_thread, and the LLM-context builder.
- **`apps/integrations/flight_lookup.py:229-242 (the time.sleep is on line 240)`** — _Slow queries / blocking (6) and No magic numbers (3)_  
  On a 429 the retry does a blocking time.sleep(1.3) inside a synchronous Django request thread, tying up a worker (and the lookup→departures rescue fires multiple back-to-back calls, so several sleeps can stack on one user click), with the pause being a bare magic literal.  
  **Fix:** Extract the pause to a module-level named constant, e.g. RATE_LIMIT_RETRY_PAUSE_SECONDS = 1.3, and use it on line 240. Optionally add an inline comment that this briefly blocks the request thread. The existing docstring already explains the per-second rate-limit rationale; the retry-once cap already bounds per-call blocking, so no behavioral change (e.g. async/'retry later' signal) is warranted for this internal, button-triggered, single-user tool.
- **`apps/integrations/flight_lookup.py:297-332`** — _Testability & code quality (10)_  
  When there is no time hint the two day-windows (0-11, 12-23) are fetched and their departures simply concatenated, then truncated at CANDIDATE_LIMIT without any sort, so the morning window always wins and afternoon/evening flights are silently dropped — the 'best 5 candidates' can exclude the flight that actually matches the client's loss time.  
  **Fix:** In the no-time-hint branch, avoid the morning-window-always-wins bias when truncating to CANDIDATE_LIMIT. Either (a) sort the combined departures by movement.scheduledTime before the candidate loop so the cap reflects chronological coverage rather than fetch order, or better (b) interleave/round-robin across the two windows (or raise the cap for the no-hint case) so afternoon/evening flights aren't categorically dropped. When time_hint is present, sorting candidates by proximity to time_hint before truncating further sharpens results. A small unit test feeding >5 morning + several afternoon departures with no hint would lock in the fix.
- **`apps/integrations/flight_lookup.py:370-372`** — _Testability & code quality (10)_  
  The public service function analyze_flight_match has no return type annotation and its core `claim` parameter is untyped, while sibling functions in the same module are fully annotated, making the optional-FlightCheck contract implicit.  
  **Fix:** Annotate the signature, e.g. `def analyze_flight_match(claim: 'Claim | None', flight_payload: Optional[Dict[str, Any]] = None, candidates: Optional[List[Dict[str, str]]] = None, flight_details_text: str = '') -> Optional[FlightCheck]:`.
- **`apps/integrations/flight_lookup.py:266-267`** — _Testability & code quality (10)_  
  The helper _fetch_departures_window is a public-ish service function (multi-branch HTTP/error handling) with a fully typed signature but no docstring, unlike every other function in the module which documents its return-shape contract.  
  **Fix:** Add a one-line docstring describing the return contract ({} on 204/404 'no data', None on transport/provider error, dict on success) to match the rest of the module.
- **`apps/integrations/flight_lookup.py:245-263`** — _Separation of concerns / DRY (2,4)_  
  lookup_flight and _fetch_departures_window each independently re-implement the same FlightProviderNotConfigured-reraise / HTTPError-404-to-empty / generic-Exception-to-None error mapping around _aerodatabox_get, duplicating the provider error-translation policy in two places.  
  **Fix:** Extract the shared error-mapping into a small wrapper (e.g. a decorator or helper that takes the empty-result sentinel) so both callers translate provider/transport errors identically and the policy lives in one spot.
- **`apps/integrations/briefing.py:79-108`** — _Testability & code quality (10)_  
  generate_claim_summary, a public service entry point, has no type hints on its `claim`/`ticket_data` params or its `Optional[str]` return, whereas the sibling refresh_claim_summary right below it is annotated `-> bool`.  
  **Fix:** Add annotations: `def generate_claim_summary(claim, ticket_data: dict) -> Optional[str]:` (and a Claim type where importable without a cycle).

### communications (14)

- **`apps/communications/services.py:406-490 (try at 406; dead `except json.JSONDecodeError` block at 470-490)`** — _Testability & Code Quality (10) — dead code / wrong exception type_  
  The try/except in parse_ai_response guards against json.JSONDecodeError, but json.loads is already done above (lines 387-400) and the guarded body never calls json.loads, so the entire fallback block (lines 470-490) is unreachable dead code while real errors (e.g. a non-dict payload causing TypeError) escape uncaught.  
  **Fix:** Remove the unreachable `except json.JSONDecodeError` block (lines 470-490) entirely. Immediately after the `if data is None: return result` guard (after line 404), add `if not isinstance(data, dict): logger.warning(...); return result` so a scalar/array JSON reply returns defaults instead of raising TypeError at `'summary' in data`. The text-inference logic already exists inline at lines 430-442 for the no-category case, so the deleted fallback's behavior is largely preserved; if the first-line-as-summary behavior from the dead block (lines 485-488) is still desired for non-dict replies, fold it into the new isinstance guard.
- **`apps/communications/services.py:402-410 (insert guard after line 402; TypeError originates at lines 408-410)`** — _Django Best Practices (7) — unhandled type error in a service path_  
  parse_ai_response assumes the parsed `data` is a dict; json.loads of a valid JSON array or scalar succeeds, then `key in data` / `data[key]` raises TypeError that is not caught (the only except is json.JSONDecodeError), so a misshapen-but-valid-JSON LLM reply crashes the parser instead of degrading gracefully.  
  **Fix:** After the `if data is None: ... return result` block (line 404), add a type guard before entering the extraction try-block: `if not isinstance(data, dict): logger.warning(f"AI response JSON was not an object ({type(data).__name__}): {raw_response[:100]}"); return result`. Optionally also broaden the except at line 470 to `except (json.JSONDecodeError, TypeError, KeyError)` as defense-in-depth, but the isinstance guard is the clean primary fix and makes the dict assumption explicit.
- **`apps/communications/services.py:162-163, 254, 675, 681, 998`** — _DRY (2) — duplicated regex_  
  The plain email-extraction regex r'[\w\.-]+@[\w\.-]+\.\w+' is hand-written inline in extract_alias_from_headers (254), process_single_email twice (675, 681) and _first_email_in_header (998), while two near-identical compiled variants (_EMAIL_RE, _PLAIN_EMAIL_RE) already exist at 162-163.  
  **Fix:** Define one compiled module-level email regex and reuse it via the existing _first_email_in_header helper everywhere a single address is pulled from a header.
- **`apps/communications/services.py:597-603, 1118-1131`** — _DRY (2) / Idempotency (5) — duplicated dedup logic with divergent robustness_  
  The Message-ID dedup (read message_id, [:512], EmailLog.objects.filter(message_id=...).exists() skip) is copy-pasted in both flows, but only check_email_for_ticket additionally catches IntegrityError from the unique constraint; process_single_email's EmailLog.objects.create (line 713) has no IntegrityError guard and relies on the broad `except Exception` at 785 returning None.  
  **Fix:** Extract a shared `is_duplicate(message_id)` / dedup helper, and add an explicit IntegrityError catch around the create in process_single_email so a constraint race is treated as a clean dedup skip rather than a generic processing error.
- **`apps/communications/services.py:566-572`** — _Django Best Practices (7) — docstring after non-docstring statement_  
  process_single_email opens with a multi-line comment block (lines 573-579) placed BEFORE the docstring (lines 580-592), so the triple-quoted block is not the function's __doc__ and the explanatory NB comment ends up between the signature and the actual docstring.  
  **Fix:** Move the docstring to be the first statement in the function body and demote the NB rationale to a comment inside or above the docstring.
- **`apps/communications/services.py:33, 1085`** — _No Magic Numbers/Strings (3) / DRY (2) — alias validation duplicated and divergent_  
  The alias is validated with two different inline regexes in two places: check_email_for_ticket uses r'[\w.+-]+@[\w-]+(\.[\w-]+)+' (1085) while the InvalidAlias docstring (65-70) documents IMAP-interpolation safety, yet search_alias_uids just wraps the alias in quotes (973) with no shared validation, so the safety contract depends on callers always going through check_email_for_ticket first.  
  **Fix:** Hoist the alias-validation regex to a named module constant (or a validate_alias() helper) and call it inside search_alias_uids / open path as well so the IMAP-interpolation guard cannot be bypassed.
- **`apps/communications/services.py:987-993, 1014-1072`** — _Testability & Code Quality (10) — missing type hints / return-type vagueness_  
  Several public-ish service helpers lack precise type hints: _ai_tags_for returns `set` (untyped element), _process_ticket_email's `claim` param is untyped, and call_qwen_ai/_process_ticket_email return bare Dict[str, Any] making the contract loose for callers and tests.  
  **Fix:** Add `set[str]` / `Optional[Claim]` annotations and consider a TypedDict for the categorization result so the parsed-dict shape (summary/category/action_required/auto_resolvable) is explicit and testable.
- **`apps/communications/views.py:24 (base queryset) and 35-55 (get_queryset), with the triggering reads in serializers.py lines 9-10`** — _Slow queries / N+1 (6)_  
  EmailLogViewSet never select_related('claim') yet the serializer reads claim.id and claim.status (and __str__ reads claim), so listing N emails fires N extra queries for the related Claim.  
  **Fix:** In EmailLogViewSet.get_queryset (apps/communications/views.py), change `queryset = super().get_queryset()` to `queryset = super().get_queryset().select_related('claim')` so the related Claim is fetched in a single JOIN, eliminating the per-row queries triggered by the serializer's `source='claim.id'` and `source='claim.status'` fields. (Optionally set the class-level `queryset = EmailLog.objects.select_related('claim')` instead.)
- **`apps/communications/client_updates.py:139, 156, 179-180, 186, 193, 233, 349, 365, 373, 386-387, 441`** — _No magic strings (3) / DRY (2)_  
  ClientUpdate state values ('SCHEDULED','DRAFTED','SENT','SKIPPED') and the milestone 'FINAL' are repeated as bare string literals across many filter/update calls instead of using ClientUpdate.STATE_CHOICES / a named constant, so a typo silently no-ops a filter and renames require hunting every literal.  
  **Fix:** Add named state constants on the model (e.g. ClientUpdate.STATE_SCHEDULED='SCHEDULED', STATE_DRAFTED='DRAFTED', STATE_SENT='SENT', STATE_SKIPPED='SKIPPED', defining STATE_CHOICES from them) and replace the raw 'SCHEDULED'/'DRAFTED'/'SENT'/'SKIPPED' literals throughout client_updates.py (lines 139, 156, 179-180, 186, 193, 233, 349, 365, 373, 386-387, 441) with those constants, mirroring how milestone comparisons already use the imported FINAL_MILESTONE constant. Low priority cleanup, not blocking.
- **`apps/communications/client_updates.py:354-368`** — _Idempotency (5) / Atomicity (9)_  
  send_follow_up posts the public Zendesk reply (external side-effect) before writing local SENT state and without transaction.atomic, so if the post succeeds but the subsequent save() raises, a later retry re-posts the same client reply (double-send); the only guards are the prior _claim_due_update CAS and the in-memory update.state=='SENT' check.  
  **Fix:** Keep the external-call-then-save order but wrap the post+save so a save failure is logged loudly, or record an outbound idempotency marker (e.g. set sent_at/state in the same row via select_for_update) before/with the post; at minimum document that double-send on save failure is accepted.
- **`apps/communications/client_updates.py:161-173`** — _Atomicity & data integrity (9)_  
  start_client_updates does a claim.save() (writing client_report_draft) and then schedule_next() which creates a ClientUpdate row — two writes across two models with no transaction.atomic, so a crash between them leaves a drafted report with no scheduled cadence.  
  **Fix:** Wrap the claim.save() + schedule_next(...) pair in transaction.atomic().
- **`apps/communications/client_updates.py:393-445`** — _Testability & code quality (10)_  
  run_due_updates is ~50 lines mixing flag-check, queue iteration, ownership CAS, close/found/final branching, send, and failure-revert — hard to unit-test the branches in isolation.  
  **Fix:** Extract the per-update decision (close/hold/send/retry) into a small helper like _process_due_update(update, now) -> outcome and let run_due_updates just tally the outcomes.
- **`apps/communications/client_updates.py:411, 302-303, 409`** — _Slow queries / N+1 (6)_  
  SystemSettings.get_instance() runs an uncached get_or_create(pk=1) DB hit, and it is called once per due update inside the run_due_updates loop (via prepare_follow_up -> _draft_follow_up), re-fetching the same singleton on every iteration.  
  **Fix:** Fetch SystemSettings.get_instance() once at the top of run_due_updates and thread the autosend flag / ai_api_key down, or pass the settings instance into prepare_follow_up/_draft_follow_up.
- **`apps/communications/management/commands/run_client_updates.py:39-42`** — _Slow queries / N+1 (6)_  
  The --dry-run loop reads u.claim.alf_claim_id per update; due_updates() does select_related('claim') so the FK itself is prefetched, but this is only safe because of that select_related — worth a guard if the queryset ever changes.  
  **Fix:** No code change required today; keep due_updates()'s select_related('claim') and add a brief comment that the dry-run printout depends on it to avoid a future N+1 regression.

### users (10)

- **`apps/users/views.py:45-70 (decorator), 76 (invocation), 93 (cache.set TTL)`** — _NO MAGIC NUMBERS/STRINGS (3)_  
  The rate_limit_logins decorator declares a `timeout` parameter but never uses it; the cache TTL for the failed-attempt counter is hardcoded as the literal 60 in login_view (line 93), so changing the decorator's timeout has no effect.  
  **Fix:** Add module-level constants near the top of apps/users/views.py, e.g. `LOGIN_THROTTLE_MAX_ATTEMPTS = 5` and `LOGIN_THROTTLE_WINDOW = 60`. Default the decorator to them (`def rate_limit_logins(max_attempts=LOGIN_THROTTLE_MAX_ATTEMPTS, timeout=LOGIN_THROTTLE_WINDOW):`) and actually thread `timeout` through — but since the counter is written in the view, not the decorator, the cleanest fix is to drop the now-unused `timeout` param from the decorator entirely and reference `LOGIN_THROTTLE_WINDOW` in the view: `cache.set(cache_key, cache.get(cache_key, 0) + 1, LOGIN_THROTTLE_WINDOW)`. Then invoke as `@rate_limit_logins(max_attempts=LOGIN_THROTTLE_MAX_ATTEMPTS)`. This removes the misleading dead parameter and the duplicated literal so the window/cap live in one place.
- **`apps/users/views.py:1043-1077`** — _THIN VIEWS / FAT SERVICES (1)_  
  manager_users performs user-creation business logic in the view: manual uniqueness check, password-strength validation, and create_user call with conditional flash handling, rather than delegating to a form/service.  
  **Fix:** Extract a UserCreationForm subclass (or a small create_staff_user(username, email, password, first_name, last_name) service) so the view binds the form, calls form.is_valid()/form.save(), and renders — uniqueness and password validation come for free from the form. Optional cleanup, not urgent for an internal trusted-staff tool.
- **`apps/users/views.py:695`** — _DRY / NO MAGIC STRINGS (2,3)_  
  The open-dispute filter hardcodes the status list ['RESOLVED_WON', 'RESOLVED_LOST', 'ACCEPTED'], which is exactly Dispute.TERMINAL_STATUSES defined in apps/payments/models.py:179 — duplicated literal that will silently drift if the model constant changes.  
  **Fix:** Replace the hardcoded list at apps/users/views.py:695 with the model constant: `_open_dispute = ~models.Q(status__in=Dispute.TERMINAL_STATUSES)`. Dispute is already imported at line 682 and the identical pattern is already used at line 690, so this is a zero-risk one-line change that makes the open-dispute filter follow the documented single source of truth.
- **`apps/users/views.py:222-225, 824-827`** — _DRY (2)_  
  The status_choices distinct-values queryset is copy-pasted verbatim in agent_claims and manager_claims (the exact same 4-line Claim.objects.exclude(status='').values_list(...).distinct().order_by('status') chain).  
  **Fix:** Extract a small helper, e.g. _claim_status_choices() in this module or on the Claim manager, and call it from both views.
- **`apps/users/views.py:822-823, 951-952`** — _DRY (2)_  
  The zd_ticket_base URL construction (f'https://{zd_subdomain}.zendesk.com/agent/tickets/' if zd_subdomain else '') is duplicated across manager_claims and manager_refunds with the same SystemSettings.get_instance().zd_subdomain lookup.  
  **Fix:** Add a helper or a property on SystemSettings (e.g. settings.zendesk_ticket_base) and reuse it everywhere.
- **`apps/users/views.py:307-313`** — _TESTABILITY & CODE QUALITY / DEAD CODE (10)_  
  _followup_or_403 is named and documented as if it 'enforces the same assignment guard' and can return a redirect (callers check `if update is None: return claim`), but after the role removal it does no guard at all and can never return None — the docstring and every caller's None-branch are dead/misleading.  
  **Fix:** Either inline the get_object_or_404 in each caller or rename to _followup_and_claim and drop the unreachable `if ... is None` branches and the stale '403/assignment guard' docstring.
- **`apps/users/views.py:462-535`** — _ATOMICITY & DATA INTEGRITY (9)_  
  agent_upload_evidence is wrapped in @transaction.atomic, but it writes an uploaded file to disk via tempfile.NamedTemporaryFile(delete=False) and os.unlink inside the transaction; on some error paths (e.g. the bare `except Exception` at line 533 after the temp file is created but before unlink) the temp file in the OS temp dir can be leaked, and filesystem side-effects are not covered by the DB transaction anyway.  
  **Fix:** Use NamedTemporaryFile without delete=False (context-managed) or wrap unlink in try/finally so the temp file is always removed; the atomic decorator only protects the single ClaimEvidence.create, so consider whether it is even needed here.
- **`apps/users/views.py:595, 627`** — _DJANGO BEST PRACTICES / SLOW QUERIES (6,7)_  
  Comments claim SystemSettings.get_instance() is 'cached', but get_instance (apps/config/models.py:403) does an uncached get_or_create(pk=1) DB hit on every call; manager_claims/manager_refunds/agent_emails each call it at least once per request, so the 'cached' comment is misleading.  
  **Fix:** Either actually memoize the singleton (e.g. cache.get_or_set) in get_instance, or remove the inaccurate 'cached' comments to avoid misleading future readers.
- **`apps/users/views.py:108-112`** — _TESTABILITY & CODE QUALITY / DEAD CODE (10)_  
  dashboard_redirect re-implements an auth check and redirect, but it is mapped to the '' path and simply forwards to manager_dashboard which is already @login_required — and after the single-user-type change login_view also always redirects to manager_dashboard, making the 'role-based redirect' docstring on login_view stale.  
  **Fix:** Low priority: drop the redundant is_authenticated check (let @login_required on manager_dashboard handle it) and update login_view's 'role-based redirect' docstring to reflect the single dashboard.
- **`apps/users/models.py:4-10`** — _Django best practices (7)_  
  The custom User model defines no Meta.ordering, so any future User queryset evaluated without an explicit order_by (e.g. in a paginated admin/list view) will be returned in an undefined database order.  
  **Fix:** Add an inner Meta class with ordering = ('-date_joined',) (or ('username',)) so listings are deterministic by default; current call sites already happen to order explicitly, so this is purely defensive.

### config (9)

- **`apps/config/encrypted_fields.py:67-76 (EncryptedCharField.from_db_value); same pattern repeats at 96-105 in EncryptedTextField`** — _Security (8) / Code quality (10)_  
  from_db_value swallows any decryption failure and returns '' (empty string) instead of the value, so a key rotation or corrupted ciphertext silently presents credentials as 'not configured' rather than surfacing the failure, and a subsequent save would persist that empty value, destroying the credential.  
  **Fix:** Distinguish 'undecryptable' from 'empty' rather than silently returning ''. Simplest robust option: in from_db_value, on decrypt failure return the original ciphertext (or a non-empty sentinel) and log at error level, so the UI shows the field is set-but-broken and a subsequent full-instance save round-trips the original ciphertext instead of overwriting it with ''. Apply the same change to both EncryptedCharField (67-76) and EncryptedTextField (96-105). Optionally, harden the only full-instance save site (apps/users/views.py:978/987) to use update_fields scoped to the actually-edited fields so credential columns are never rewritten unless a new value was supplied.
- **`apps/config/services/connection_tester.py:20, 70, 117, 177, 307 (each `settings = SystemSettings.objects.get(pk=1)`), with duplicated `except SystemSettings.DoesNotExist` branches at 52, 92, 159, 227, 339`** — _DRY (2) / Django best practices (7)_  
  Every test_* method repeats `settings = SystemSettings.objects.get(pk=1)` with its own try/except SystemSettings.DoesNotExist block instead of using the existing SystemSettings.get_instance() classmethod (get_or_create) that makes the DoesNotExist branch impossible.  
  **Fix:** Replace the five `settings = SystemSettings.objects.get(pk=1)` calls with `settings = SystemSettings.get_instance()` and delete the five `except SystemSettings.DoesNotExist:` branches (the get_or_create singleton guarantees the row exists, and save() pins pk=1). This also brings the file in line with the rest of the codebase, which already uses get_instance() everywhere. Keep the broad `except Exception` / `except requests.RequestException` handlers as-is.
- **`apps/config/services/connection_tester.py:278-291`** — _Slow queries / atomicity (6,9) — read-modify-write race_  
  _update_status does get_or_create then mutates and saves the full object; combined with the auto_now_add bug above, and because ServiceStatus.service is unique, concurrent test runs for the same service do an unguarded read-modify-write of the same singleton-per-service row.  
  **Fix:** Optional cleanup only: collapse the get_or_create + mutate + save into a single `ServiceStatus.objects.update_or_create(service=service, defaults={'status': status, 'last_checked': timezone.now(), 'last_error': message if (not success and message) else '', 'metadata': metadata or {}})`. This is cleaner and also lets you drop the dependence on the model's auto_now_add for last_checked. Not required for correctness given the single-trusted-user, sequential-invocation reality.
- **`apps/config/models.py:20-27, 71, 77, 84, 92-98`** — _No magic strings (3)_  
  Status string literals ('connected','disconnected','error','running','stopped') are hardcoded across STATUS_CHOICES, mark_* methods, and get_status_color, and the same strings reappear in connection_tester.py and scheduler_controller.py — a typo in any copy silently mismatches.  
  **Fix:** Define module-level constants (e.g. STATUS_CONNECTED = 'connected', ...) or a TextChoices class and reference them everywhere instead of bare literals.
- **`apps/config/services/connection_tester.py:15, 248`** — _No magic numbers (3)_  
  Timeout (10 seconds) and the scheduler STALE_AFTER (2 hours) are bare literals embedded in code; STALE_AFTER in particular is a business threshold that the cron cadence depends on.  
  **Fix:** Promote self.timeout = 10 and STALE_AFTER = timedelta(hours=2) to named module constants (and ideally keep STALE_AFTER near the cron interval definition so they stay in sync).
- **`apps/config/api/views.py:82-95`** — _Thin views / fat services (1)_  
  toggle_service performs the non-scheduler branch's DB read-modify-write (get_object_or_404, set is_enabled, save) and response shaping inline in the view, while the SCHEDULER branch correctly delegates to SchedulerController — asymmetric layering.  
  **Fix:** Move the generic toggle into a small service method (e.g. ConnectionTester/ServiceStatus helper or a toggle_service_enabled service) so both branches go through a service layer.
- **`apps/config/api/views.py:115-119`** — _Thin views / fat services (1) / Django best practices (7)_  
  toggle_setting_flag parses the boolean inline via BooleanField.TRUE_VALUES (an import inside the function) and mutates SystemSettings directly in the view rather than validating through a serializer/service like the sibling toggle endpoints.  
  **Fix:** Reuse a serializer (e.g. extend ToggleSerializer with a flag field validated against TOGGLEABLE_SETTING_FLAGS) for parsing/validation and move the setattr/save into a service method; lift the local BooleanField import to module top.
- **`apps/config/encrypted_fields.py:91-117`** — _Code quality / dead code (10)_  
  EncryptedTextField is defined and imported in models.py but is never used as an actual model field (grep shows no field declaration uses it); it is also missing the deconstruct() max_length-stability fix that EncryptedCharField has (though TextField has no max_length, so the asymmetry is only a maintenance smell).  
  **Fix:** Either remove EncryptedTextField (and its import) as dead code, or, if kept for future use, document that and ensure parity with EncryptedCharField's deconstruct handling.
- **`apps/config/encrypted_fields.py:23-28`** — _Security (8)_  
  The PBKDF2 salt is derived deterministically from the key itself (sha256 of a constant prefix + first 16 bytes of the secret), so it is effectively a fixed per-key salt rather than a random stored salt; this is acceptable for this single-tenant internal tool but weakens KDF guarantees and is non-obvious.  
  **Fix:** Document the trade-off (it must be deterministic so existing ciphertext stays decryptable), or migrate to a stored random salt per deployment; at minimum keep the checks.py W001 warning prominent so ENCRYPTION_KEY is set deliberately.

### agent (9)

- **`apps/agent/services.py:313-403`** — _Slow queries / N+1 (6)_  
  fetch_context loops over claim_ids and, inside the loop, issues per-claim DB queries (EmailLog, Refund, timeline) plus TWO blocking external Zendesk HTTP calls (fetch_zendesk_ticket + fetch_zendesk_comments) per claim, so latency scales linearly with the number of claims and external API round-trips.  
  **Fix:** Batch the local querysets to remove the N+1: fetch all claims with EmailLog/Refund/timeline via EmailLog.objects.filter(claim__in=claims), Refund.objects.filter(claim__in=claims), and prefetch_related('updates'), then group in Python by claim. For the external Zendesk calls (the costlier part), add a small per-request cap on how many claims trigger Zendesk fetches (e.g. skip or lazy-load beyond ~3), since each is a blocking up-to-30s round-trip; concurrency isn't worth the complexity given the typical single-claim input.
- **`apps/agent/services.py:289-416`** — _Testability & code quality (10)_  
  fetch_context is a single ~125-line function that fetches and reshapes claims, emails, refunds, timeline, and Zendesk data, building five parallel dicts in one body — well over the ~40-line guideline and mixing many concerns, which makes it hard to test in isolation.  
  **Fix:** Optionally extract the four per-claim sub-fetches into focused private helpers — _email_context(claim), _refund_context(claim), _timeline_context(claim), _zendesk_context(claim) — each returning its list/dict, and have fetch_context's loop compose them and append the corresponding 'sources' entries. Optionally add a TypedDict for the context shape. This is a low-priority readability/testability cleanup, not a bug fix; defer unless touching this code anyway.
- **`apps/agent/services.py:87-92`** — _Django best practices / latent bug (7)_  
  The optional claim_ids argument (DB primary keys per the view docstring's claimIds) is coerced with str(cid) and then matched against the alf_claim_id CharField ('ALF…' format) in fetch_context, so a real integer PK would never match and would silently resolve to a 'not found' claim; the path is currently dead because the view never passes claim_ids, but it is a latent bug if wired up.  
  **Fix:** Either look up provided numeric claim_ids by pk (Claim.objects.filter(pk__in=claim_ids)) and the detected string IDs by alf_claim_id separately, or drop the unused claim_ids parameter entirely until the view actually sends it.
- **`apps/agent/services.py:418-486`** — _Testability & code quality / dead code (10)_  
  _handle_multiple_claims and _handle_no_claim_detected are never called by process_message or any production code path — they are only exercised by tests, so they are effectively dead code kept alive solely by their own tests.  
  **Fix:** Either wire these helpers into process_message's flow (e.g. return _handle_no_claim_detected when no claim resolves, _handle_multiple_claims when search yields several) or delete them and their tests.
- **`apps/agent/services.py:162-171`** — _No magic numbers/strings (3)_  
  process_message passes bare literals temperature=0.7 and max_tokens=2000 (and the history window 10 / claim cap 5 elsewhere) directly into the AIClient call, so tuning these chat-specific values requires editing the method body instead of a named constant.  
  **Fix:** Promote these to named class constants (e.g. CHAT_TEMPERATURE = 0.7, CHAT_MAX_TOKENS = 2000, HISTORY_WINDOW = 10) on AgentChatService.
- **`apps/agent/services.py:418-430`** — _Testability & code quality / type-shape mismatch (10)_  
  _handle_multiple_claims is type-hinted context: Dict and indexes context['claims'] expecting a list of Claim model objects (accessing c.alf_claim_id), whereas fetch_context returns context['claims'] as a list of dicts; the two 'context' shapes are incompatible, masking the dead-code/refactor hazard behind a loose Dict hint.  
  **Fix:** Give the two context structures distinct, explicit types (TypedDict or dataclass) so the model-object vs dict mismatch surfaces in static analysis instead of at runtime.
- **`apps/agent/services.py:409-414`** — _Security / info disclosure (8)_  
  On an unexpected exception while fetching context, the raw exception text is placed into context['claims'][...]['error'] = f'Error fetching claim data: {str(e)}', which is then included in data sent to the LLM and surfaceable in the response, potentially leaking internal error details (and unredacted PII from the exception) outside the trust boundary.  
  **Fix:** Log the exception server-side (logger.error with exc_info) but store a generic, fixed error string in the context dict rather than str(e).
- **`apps/agent/views.py:22-49`** — _Django best practices / dead contract (7)_  
  The AgentChatAPIView docstring documents a claimIds body field, but post() never reads request.data.get('claimIds') and never forwards claim_ids to service.process_message, so the documented API contract silently does nothing.  
  **Fix:** Either parse claimIds from request.data and pass it through to process_message (after fixing the pk-vs-alf_claim_id lookup), or remove claimIds from the docstring to avoid a misleading contract.
- **`apps/agent/services.py:51`** — _Testability & code quality (10)_  
  process_message is missing a type hint on its return-path branches versus a single ChatResponse contract is fine, but the function is ~140 lines doing claim-ID detection, name/email fallback, history scanning, context fetching, trusted/untrusted payload assembly, and the LLM call inline — exceeding the ~40-line guideline and mixing detection with payload-building concerns.  
  **Fix:** Extract the trusted/untrusted/aliases payload assembly (lines 94-154) into a helper like _build_llm_payload(message, context, conversation_history) so process_message reads as orchestration.

### claims (8)

- **`apps/claims/views.py:278-288`** — _Atomicity & Data Integrity (9)_  
  The Zendesk refresh writes the Claim (save) and then a ClaimUpdateTimeline (create) as two separate DB writes with no transaction.atomic(), so a crash between them leaves a saved claim with no timeline row.  
  **Fix:** Wrap the field-update save (line 278) and the ClaimUpdateTimeline.objects.create (lines 282-288) in a single with transaction.atomic(): block so the audit row is never orphaned from the field updates. Keep fetch_zendesk_ticket/fetch_zendesk_comments/analyze_zendesk_ticket_for_claim outside the block. Note refresh_claim_summary does its own claim.save (briefing.py:118) and makes an LLM call, so leave it between/outside the atomic block as the fix suggests; the ai_summary save will remain a separate write, which is acceptable since it is recomputable. Low priority given the manual, retriable, single-user nature of the endpoint.
- **`apps/claims/models.py:249-270`** — _Slow queries / N+1 (6)_  
  Claim.has_refund / refund_total / latest_refund / refund_status each hit the DB and are used as serializer-adjacent properties; if rendered per-row in a list they fire separate queries per claim (the ViewSet prefetches 'evidence'/'emails' but never 'refunds').  
  **Fix:** If these properties are read in any list context, add prefetch_related('refunds') in get_queryset and compute refund_total in Python over the prefetched cache (or annotate); otherwise document them as detail-only to avoid accidental N+1.
- **`apps/claims/models.py:258`** — _No magic numbers/strings (3)_  
  The refund status filter uses the bare string 'COMPLETED' which is the Refund.status choice value defined over in apps/payments/models.py, duplicating that literal across apps with no shared constant.  
  **Fix:** Import/reference the canonical choice (e.g. Refund.Status.COMPLETED or a module-level constant in payments) instead of hardcoding 'COMPLETED' in the claims model.
- **`apps/claims/models.py:255-259`** — _Testability & Code Quality (10)_  
  refund_total returns the int literal 0 when there are no completed refunds but a Decimal when there are, giving callers a mixed return type (Decimal | int) for a money value.  
  **Fix:** Return Decimal('0.00') as the fallback (result['total'] or Decimal('0.00')) so the property is always a Decimal.
- **`apps/claims/views.py:178-203`** — _Django best practices (7)_  
  ClaimEvidenceViewSet.perform_create fetches the claim with a bare Claim.objects.get(...) and the docstring claims AGENTs may only upload to claims assigned to them — but no ownership check exists (correct post-role-removal) and the get() pattern duplicates the get_object_or_404 idiom used elsewhere.  
  **Fix:** Either move claim resolution into the serializer (PrimaryKeyRelatedField on 'claim') so DRF validates existence, or use get_object_or_404; and delete the now-false 'AGENTs may only upload to claims assigned to them' sentence from the docstring to avoid implying a missing access check.
- **`apps/claims/views.py:80-85`** — _Testability & Code Quality (10)_  
  destroy() catches bare Exception and maps every failure to a generic 500, which can swallow programming errors (e.g. a typo'd attribute) under a misleading 'Error deleting claim.' message; same pattern repeats in proof_of_work and evidence destroy.  
  **Fix:** Catch only the expected DB/integrity exceptions and let unexpected ones propagate to DRF's handler (which logs with traceback), or at minimum log with exc_info=True so the real stack trace is captured.
- **`apps/claims/views.py:89-117`** — _Atomicity & Data Integrity (9)_  
  bulk_delete iterates Claim.objects.filter(id__in=ids) and deletes inside the loop; for each claim it also runs claim.emails.update(claim=None) — fine functionally, but the per-claim emails.update + delete each issue queries in a loop with no select_related, and the whole bulk op is not a single transaction (partial completion possible if the process dies mid-loop).  
  **Fix:** This is acceptable for an internal cleanup tool, but consider documenting the intentional per-claim transaction boundary, or wrapping the whole sweep so a mid-loop crash doesn't leave a partial result that the caller can't distinguish from a finished one.
- **`apps/claims/services.py:13-41 (validation); consumed at apps/payments/document_service.py:122-124 raw embed, decoded by WeasyPrint at render`** — _Validate untrusted media by content not name, and bound resource use before fully decoding it [completeness pass]_  
  validate_evidence_image enforces a 10MB byte cap and verifies real image bytes (magic), but imposes no pixel/dimension ceiling, so a within-limit decompression-bomb image is decoded full-size into RAM by WeasyPrint when building the dispute PDF.  
  **Fix:** After PIL verify, also reject images whose width*height exceeds a sane bound (e.g. open and check img.size, or lower Image.MAX_IMAGE_PIXELS and catch DecompressionBombError); optionally downscale staff evidence on the embed path the way fetched comment images already are.

### ai (8)

- **`apps/ai/client.py:196-200 (the offending statement is line 200: `return type(obj).model_validate(data)`)`** — _Testability & Code Quality (10) / DRY (2)_  
  _untokenize_model re-validates the untokenized data through type(obj).model_validate(data), which re-runs every field_validator (e.g. _cap_summary/_cap_explanation _trim caps); restored PII placeholders expand from ~16 chars to full emails/names and can push a field past its soft cap, silently truncating real content (and appending an ellipsis mid-value).  
  **Fix:** In _untokenize_model, reconstruct without re-validation: build `data = obj.model_dump()`, run `_untokenize_in_place(data, ...)`, then `return type(obj).model_construct(**data)` so the soft-cap (_cap_summary/_cap_explanation) and hard max_length validators do not re-run on the restored PII. (Note model_construct does not recurse into nested BaseModel children like EvidencePlacement, so if those need reconstruction, rebuild the nested items explicitly — or, simpler and safest, apply the length caps only to the tokenized text before untokenizing so restored PII can never be truncated.)
- **`apps/ai/client.py:36-42`** — _Django Best Practices (7) / Code Quality (10)_  
  _resolve_salt swallows all exceptions from SystemSettings.get_instance() with a bare `except Exception: pass`, so a real DB/config error is silently masked and the code falls through to the env var (or raises a misleading 'not configured' error) instead of surfacing the actual failure.  
  **Fix:** Narrow the catch to the operational/decryption errors actually expected and log before falling through, e.g. `except (django.db.Error, ValueError) as exc: logger.warning("SystemSettings salt lookup failed, falling back to env var: %s", exc)`. Keep the env-var fallback and the final AIClientError raise unchanged so behavior is identical but a genuine DB/decryption failure is observable in logs.
- **`apps/ai/client.py:186-192`** — _Code Quality (10)_  
  The success log labels character counts as token counts: tokens_in is len(messages[1]['content']) (characters) and tokens_out is len(raw_reply) (characters), which is misleading for cost/usage monitoring when the OpenAI response exposes a real usage object.  
  **Fix:** Log completion.usage.prompt_tokens / completion_tokens from the API response, or rename the fields to chars_in/chars_out to avoid implying token accounting.
- **`apps/ai/client.py:173-181`** — _Code Quality (10) / Django Best Practices (7)_  
  When AI_VALIDATION_STRICT is False the lenient fallback uses `import json` inline inside the function and the more permissive model_validate(json.loads(...)) path; the inline import is dead-weight style and the lenient branch duplicates the strict failure handling that's already above.  
  **Fix:** Move `import json` to module top-level and factor the validation/fallback into a small helper to remove the duplicated AIResponseValidationError construction in both branches.
- **`apps/ai/tokenizer.py:213-218`** — _Code Quality (10)_  
  Name-part tokenization keys the placeholder on _part.lower() but stores mapping[placeholder] = _part.capitalize(), so an ALL-CAPS occurrence like 'JOHN' is restored on untokenization as 'John', losing the original casing in the LLM output.  
  **Fix:** Store the matched substring (match.group(0)) as the mapping value instead of _part.capitalize(), preserving the original casing on restore.
- **`apps/ai/tokenizer.py:60-61`** — _No Magic Numbers/Strings (3) / Code Quality (10)_  
  The FLIGHT detector `\b[A-Z]{2}\d{2,4}\b` is broad and will tokenize any two-letter+digits token (e.g. 'CA90210', 'ID4521', container/reference codes), replacing non-flight data with <FLIGHT_..> placeholders that then round-trip — over-tokenization that can confuse the LLM and is hard to reason about with no allow/deny refinement.  
  **Fix:** Tighten the pattern (e.g. validate against a known IATA airline-code set or require a recognizable flight context), or document the over-match as accepted; at minimum extract the regex intent into a named, tested constant with examples.
- **`apps/ai/tokenizer.py:131-171`** — _Code Quality (10)_  
  _tokenize_phones collects matches across multiple regions and skips overlaps by `if start < cursor`, but does not prefer the longest/best match when two regions produce overlapping spans starting at the same/earlier index, so a worse (e.g. shorter, mis-parsed) match from an earlier region can win and a better E.164 from a later region is dropped.  
  **Fix:** When building all_matches, on span overlap keep the longest match (or the one with a valid number type); sort/resolve overlaps explicitly rather than first-come-first-served by region order.
- **`apps/ai/client.py:94-97`** — _Code Quality / Testability (10)_  
  _untokenize_model / _untokenize_in_place lack type hints on the `node` walker param and the recursive helper, and `complete` mixes tokenization, message build, HTTP call, validation, lenient fallback, untokenize, and logging in one ~110-line method — a long multi-responsibility function that is harder to unit-test in isolation.  
  **Fix:** Add explicit type hints to _untokenize_in_place(node: object, ...) and extract the validation/fallback block and the LLM-call block into small private helpers to shorten complete() and ease testing.

### lora_app (8)

- **`lora_app/settings.py:201-203 (the three keys within the DEFAULT_THROTTLE_RATES block at 198-204)`** — _NO MAGIC NUMBERS/STRINGS (3)_  
  Three custom DRF throttle rates ('login' 5/min, 'paypal_webhook' 100/hour, 'zendesk_sidebar' 30/min) are defined but no ScopedRateThrottle/throttle_scope/throttle_classes anywhere in apps/ references them, so the intended per-endpoint limits (e.g. login brute-force protection, webhook cap) are silently NOT enforced.  
  **Fix:** Delete the three unused throttle rate keys ('login', 'paypal_webhook', 'zendesk_sidebar') from DEFAULT_THROTTLE_RATES in lora_app/settings.py:201-203, since the brute-force/webhook/sidebar protections they imply are already enforced by @rate_limit_logins, the X-Webhook-Secret / PayPal signature checks, and ZendeskSidebarAuth respectively. Alternatively, if a DRF-native throttle is preferred, wire each scope onto its view via a ScopedRateThrottle subclass with throttle_scope — but this is optional and not required for security. Keep the active 'anon'/'user' default rates.
- **`lora_app/settings.py:194-204`** — _SECURITY (8)_  
  AnonRateThrottle is applied globally as a DEFAULT_THROTTLE_CLASS at 100/hour, which covers the unauthenticated PayPal webhook endpoint; a legitimate burst of PayPal deliveries (disputes + payments across many tickets) could be silently 429'd, while the purpose-built 'paypal_webhook' scope is never wired up.  
  **Fix:** Attach the dedicated paypal_webhook throttle scope to PayPalWebhookView/PayPalDisputeWebhookView and size it for real delivery bursts, so the webhook is not subject to the shared global anon bucket.
- **`lora_app/settings.py:1-305`** — _DJANGO BEST PRACTICES (7)_  
  There is no LOGGING configuration and no ADMINS/SERVER_EMAIL, so in production unhandled 500s and integration failures (PayPal/Zendesk/IMAP/LLM) are not captured or reported beyond the platform's default stdout — poor observability for a tool that drives payments and disputes.  
  **Fix:** Add a LOGGING dict (console handler at minimum, ideally an error-tracking integration) and set ADMINS + SERVER_EMAIL so mail_admins fires on 500s, or explicitly document that Railway log capture is the intended sink.
- **`lora_app/settings.py:5`** — _TESTABILITY & CODE QUALITY (10)_  
  `import os` is never used anywhere in settings.py (paths use pathlib.Path, config uses django-environ) — dead import.  
  **Fix:** Remove the unused `import os` line.
- **`lora_app/settings.py:288-304`** — _SECURITY (8)_  
  The production security block omits SECURE_REFERRER_POLICY, so the default referrer policy may leak full claim/payment URLs (which can embed identifiers) to the third-party CDN origins allowed in the CSP.  
  **Fix:** Add SECURE_REFERRER_POLICY = 'same-origin' (or 'strict-origin-when-cross-origin') to the not-DEBUG block.
- **`lora_app/urls.py:45-51`** — _DJANGO BEST PRACTICES (7)_  
  Production media is served through django.views.static.serve, which Django docs explicitly warn is inefficient and not hardened for production; it is login-gated and the tradeoff is documented, but it remains a synchronous in-process file read for sensitive evidence images.  
  **Fix:** Acceptable short-term given the documented low-traffic internal context; plan migration of MEDIA to object storage (R2/S3) with signed, auth-checked URLs as the existing comment already notes for when traffic grows.
- **`lora_app/views.py:13-15`** — _DJANGO BEST PRACTICES (7)_  
  custom_500 renders 500.html through the full template engine; if the original 500 was caused by a template/context-processor failure (e.g. DB down affecting the auth context processor), rendering 500.html via render() can raise again and mask the real error with an opaque secondary failure.  
  **Fix:** Render the 500 page defensively without request-bound context processors (render_to_string on a minimal context, or a hardcoded HttpResponse fallback), since the 500 handler must not depend on app state.
- **`apps/users/views.py:452-539`** — _Thin views / fat services — request handlers should delegate domain logic (here, file validation) to a reusable service or form, not inline it; duplicated validation also risks the three call sites diverging. [completeness pass]_  
  agent_upload_evidence inlines the entire evidence-image validation pipeline (size, extension allowlist, libmagic MIME sniff, filetype temp-file sniff, filename sanitization, nested if/else) in the view while a reusable apps/claims/services.validate_evidence_image already exists and is used by the API and payments paths.  
  **Fix:** Extract the validation into apps/claims/services (extend validate_evidence_image to absorb the libmagic/filetype byte-sniff and reuse EVIDENCE_MAX_BYTES / EVIDENCE_ALLOWED_EXTENSIONS), then have the view call the shared validator and catch ValidationError, leaving the view to handle only request/response and the ClaimEvidence.objects.create.

### core (6)

- **`apps/core/management/commands/seed_test_data.py:186-190 (Claim); same defect at EmailLog ~265-280 received_at and Refund ~408-419 created_at/updated_at`** — _Django best practices (7)_  
  Claim.created_at/updated_at use auto_now_add/auto_now, so the explicit created_at=created_at and updated_at=created_at passed to Claim.objects.create() are silently ignored — every seeded claim collapses to "now" instead of the intended staggered -10/-8/-6/-4 day offsets.  
  **Fix:** auto_now_add/auto_now fields cannot be set via create(); they are overwritten in pre_save. After creating each row, force the timestamps with a queryset update that bypasses pre_save. For claims: Claim.objects.filter(pk=claim.pk).update(created_at=created_at, updated_at=created_at). Apply the same pattern to EmailLog (received_at) and Refund (created_at, updated_at) — leave the plain DateTimeFields (Refund.processed_at, Dispute.transaction_date/seller_response_due) as normal create() kwargs since those work. Low priority since it only affects dev seed data realism.
- **`apps/core/management/commands/seed_test_data.py:263-280 (received_at computed at 263; passed to create() at 271)`** — _Django best practices (7)_  
  EmailLog.received_at is auto_now_add=True, so the random historical received_at (1-15 days ago) handed to EmailLog.objects.create() is discarded and every seeded email is stamped "now", defeating the date-distribution this command is trying to build.  
  **Fix:** Drop received_at from the EmailLog.objects.create() call (it is ignored anyway), then immediately set it via a queryset update that bypasses pre_save: EmailLog.objects.filter(pk=email.pk).update(received_at=received_at). For consistency, build raw_headers from the same received_at value (it already does). Optionally refresh_from_db(fields=["received_at"]) the in-memory instance if downstream code in this command relies on the value. This is a non-urgent dev-tooling cleanup.
- **`apps/core/management/commands/seed_test_data.py:403-412`** — _Atomicity & data integrity (9)_  
  Refund.created_at is auto_now_add (ignored on create) but processed_at is a plain field (persists), so Refund 3's processed_at is computed from the INTENDED created_at (5 days ago + 3 = 2 days ago) while the stored created_at becomes "now" — producing a record whose processed_at precedes its created_at, an impossible ordering.  
  **Fix:** After Refund.objects.create(...), force the timestamp fields with a queryset .update() (which bypasses auto_now/auto_now_add since it does not call save()/pre_save): Refund.objects.filter(pk=refund.pk).update(created_at=created_at, updated_at=created_at). Because processed_at is already computed from the intended created_at, this makes the stored created_at match and keeps processed_at >= created_at consistent. Alternatively recompute processed_at from the stored created_at after the update. This also fixes the broader issue that created_days_ago aging is currently ignored for all seeded refunds.
- **`apps/core/management/commands/seed_test_data.py:272`** — _Testability & code quality (10)_  
  from_email uses an f-string prefix but contains no placeholders, so the f is dead and misleading (raw_headers on line 279 has the same constant repeated).  
  **Fix:** Drop the f prefix: from_email="support@lostfound.airline.com", and hoist the sender address into a single named constant reused by from_email and raw_headers.
- **`apps/core/management/commands/seed_test_data.py:186-190`** — _Testability & code quality (10)_  
  data.pop("created_offset") (and likewise claim_index/transaction_days_ago/created_days_ago pops in the dispute/refund helpers) mutates the dicts in the locally-built list in place, so a second invocation within the same process would KeyError on the already-popped keys.  
  **Fix:** Pop from a shallow copy (for data in claims_data: data = dict(data); offset = data.pop(...)) or read the key without mutating and build the create kwargs explicitly, keeping the source dicts immutable.
- **`apps/core/management/commands/run_scheduled_jobs.py:101`** — _No magic numbers/strings (3)_  
  Status string literals 'running'/'error'/'stopped' are written here as bare strings duplicating ServiceStatus.STATUS_CHOICES, so a future rename of a choice value silently desyncs the heartbeat writer from the model.  
  **Fix:** Reference named constants/choices from ServiceStatus (e.g. module-level RUNNING/ERROR/STOPPED constants or the choices tuple) instead of repeating bare strings in the command.