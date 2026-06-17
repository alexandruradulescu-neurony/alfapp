# Django Code Review — 2026-06-17

Whole-codebase review across all 9 apps, applying the 10-dimension rubric
(thin views, DRY, no magic literals, layering, idempotency, N+1, Django best
practices, security, atomicity, testability).

**Method:** 9 parallel reviewers, one per app (the two large apps — `payments`
and `integrations` — split across two reviewers each). Findings calibrated to
the project's deliberate design: **one trusted authenticated staff user** (the
manager/agent role split was removed — "missing role checks" is NOT flagged as
a security hole), and an **LLM-only PII trust boundary** (Zendesk/PayPal are
inside the trust zone). Headline HIGH findings were re-verified against source.

**Totals:** 7 distinct HIGH (8 raw — two agent findings share one root cause),
64 MEDIUM, 86 LOW.

---

## Summary table

| App | HIGH | MEDIUM | LOW | Top concern |
|-----|------|--------|-----|-------------|
| payments | 2 | 17 | 18 | PayPal `accept_claim`/`provide_evidence` not idempotent → double refund/evidence; refund webhook lacks the event-id gate the dispute webhook has |
| integrations | 0 | 12 | 25 | `views.py` is a 1500-line God module with fat webhook views; Zendesk request/error plumbing duplicated ~10× |
| communications | 1 | 8 | 9 | `send_follow_up` posts the public Zendesk reply *before* writing SENT state → duplicate client reply on retry |
| users | 1 | 13 | 10 | `logout_view` on GET (logout-CSRF, low impact); pervasive fat views; magic status strings |
| config | 1 | 5 | 9 | Singleton read-modify-write races (`toggle_*`) clobber concurrent writes; SSRF on operator-supplied probe URLs |
| core | 1 | 1 | 1 | Scheduler dispatcher has no run-lock → overlapping cron ticks double-execute side-effecting jobs |
| claims | 0 | 3 | 7 | `refund_*` model properties cause N+1 (no prefetch); cross-app status-string coupling |
| ai | 0 | 1 | 2 | Magic temperature/max_tokens defaults; double `SystemSettings.get_instance()` per call |
| agent | 1 | 4 | 5 | `client_name` never registered in `known_pii` → client names reach the LLM unredacted |

---

## Cross-cutting themes (appear in 3+ apps — fix once, not per-site)

- **Stale AGENT/MANAGER docstrings & comments** (communications, claims, config,
  users): the role split was removed but prose/permission-alias comments still
  describe role tiers. Misleads future maintainers into reintroducing roles.
- **`SystemSettings.get_instance()` uncached + read-modify-write races**
  (config, users, ai): the singleton is fetched (a DB hit) on nearly every
  request and saved as a whole row from possibly-stale in-memory copies, so
  concurrent `toggle_*`/settings saves lose updates. Cache it; use targeted
  `.filter(pk=1).update(field=...)` for single-flag writes.
- **Duplicated PayPal/Zendesk HTTP plumbing** (payments ×3 services,
  integrations ×~10 functions): hand-rolled `urllib`/`requests` + identical
  try/except ladders + repeated `timeout=getattr(settings,...,30)`. Extract one
  request helper per provider.
- **Broad `except Exception` swallowing** (integrations, communications, claims,
  ai, config): masks programming errors as "external call failed." Narrow the
  catch or keep `exc_info=True`.
- **Function-local imports scattered through view bodies** (integrations, users):
  obscure the dependency graph; hoist unless breaking a real cycle (and comment
  the ones that are).
- **Webhook idempotency is inconsistent**: `PayPalDisputeWebhookView` has an
  event-id gate; `PayPalWebhookView` (refund) and several integration webhooks
  do not, relying on unique-constraint races.

---

## payments

[SEVERITY: HIGH]
File: apps/payments/paypal_disputes_service.py
Line: 457-536
Principle: Idempotency
Issue: `accept_claim` issues a refund via PayPal with no idempotency key and no guard against the dispute already being ACCEPTED/RESOLVED, so a second invocation can refund the same claim again (verified: no status pre-check before the POST).
Fix: Re-read the dispute under `select_for_update`, bail if status is already ACCEPTED/RESOLVED_*, and pass a `PayPal-Request-Id` header so the accept-claim call is idempotent on PayPal's side.

[SEVERITY: HIGH]
File: apps/payments/paypal_disputes_service.py
Line: 330-450
Principle: Idempotency
Issue: `provide_evidence` POSTs multipart evidence to PayPal with no idempotency key and no pre-check of the local dispute status, so a double-click or post-timeout retry submits the same evidence twice.
Fix: Before the POST, re-read the Dispute under `select_for_update` and short-circuit if `status == 'EVIDENCE_SENT'`; send a stable `PayPal-Request-Id` derived from `(dispute_id, document ids)`.

[SEVERITY: MEDIUM]
File: apps/payments/refund_service.py
Line: 245-333
Principle: Atomicity & Data Integrity / Idempotency
Issue: `process_webhook_refund`'s check-then-create (`filter().first()` then `create()`) has a TOCTOU race NOT guarded by the IntegrityError-catch used in `process_woocommerce_refund`, so two concurrent PAYMENT.CAPTURE.REFUNDED deliveries can both pass the check and the second `create()` 500s on the unique constraint.
Fix: Wrap the `create()` in the same try/except IntegrityError + adopt-existing pattern already used at refund_service.py:611-640.

[SEVERITY: MEDIUM]
File: apps/payments/views.py
Line: 34-82
Principle: Idempotency
Issue: `PayPalWebhookView` has no ProcessedWebhookEvent idempotency gate (unlike `PayPalDisputeWebhookView`); a PayPal retry re-enters `process_webhook_refund` and on the concurrent path can double-insert/500.
Fix: Add the same atomic `get_or_create(event_id=...)` claim-before-side-effects gate used in `PayPalDisputeWebhookView`, with release-on-failure.

[SEVERITY: MEDIUM]
File: apps/payments/views.py
Line: 51-82
Principle: Thin Views / Fat Services
Issue: `PayPalWebhookView.post` contains event-type whitelist branching, dispatch, and response shaping plus inline secret verification — logic that belongs in the service boundary.
Fix: Move event-type dispatch into `RefundService.handle_webhook_event(data)`; keep the view a thin dispatcher.

[SEVERITY: MEDIUM]
File: apps/payments/frontend_views.py
Line: 547-624
Principle: Thin Views / Fat Services
Issue: `dispute_edit_document` holds substantial business logic: sanitize/persist HTML, version increment, conditional PDF re-render, ContentFile.save, activity-log creation.
Fix: Extract `document_service.save_edited_document(document, content_html, increment_version)`; the view just calls it.

[SEVERITY: MEDIUM]
File: apps/payments/frontend_views.py
Line: 681-753
Principle: Thin Views / Fat Services
Issue: `dispute_prepare_submission` embeds the whole prepare workflow — two action branches, draft creation, SOURCE_AI→SOURCE_AI_EDITED transitions, evidence-type defaulting, per-file validate/save loop, DB writes.
Fix: Move the generate/save orchestration and image-upload loop into a submission service.

[SEVERITY: MEDIUM]
File: apps/payments/frontend_views.py
Line: 119-195
Principle: Thin Views / Fat Services
Issue: `dispute_create` builds the entire dispute construction in the view (reason validation, email/amount/currency coercion, synthetic PayPal-id generation, atomic create + activity-log).
Fix: Extract `create_manual_dispute(claim, form_data, user)`; the view validates the form and calls it.

[SEVERITY: MEDIUM]
File: apps/payments/frontend_views.py
Line: 360-427
Principle: Thin Views / Fat Services
Issue: `dispute_link_claim` contains claim-reference lookup building, multi-match handling, PayPal transaction-id cross-check/override gate, conditional field updates, atomic save + activity-log.
Fix: Move matching + cross-check + linking into `link_dispute_to_claim(dispute, ref, override, user)` returning an outcome the view maps to messages.

[SEVERITY: MEDIUM]
File: apps/payments/paypal_disputes_service.py
Line: 423-434
Principle: Atomicity & Data Integrity
Issue: In `provide_evidence` the `status='SENT'` / `status='EVIDENCE_SENT'` writes happen only after a successful PayPal POST; a crash between POST and commit leaves PayPal holding evidence while LORA shows not-sent, and the next call re-submits.
Fix: Record a "submission attempted" marker (or the PayPal-Request-Id) before the POST and reconcile against PayPal's `evidences[]` on next sync instead of blindly re-POSTing.

[SEVERITY: MEDIUM]
File: apps/payments/refund_service.py
Line: 144-147, 370-380, 492
Principle: Atomicity & Data Integrity
Issue: In `initiate_refund`/`issue_woocommerce_refund` the post-gateway finalize (write real refund id + COMPLETED status) is a single un-retried `.save()` after the external call; if it fails after money moved, the row keeps its placeholder id and the reconciling webhook can't match it → risk of a duplicate refund record.
Fix: Harden the finalize save (retry/log-to-reconcile) or make the reconcile-by-amount path also match placeholder rows by capture_id; at minimum document the residual gap.

[SEVERITY: MEDIUM]
File: apps/payments/refund_service.py
Line: 276-282
Principle: No Magic Numbers/Strings
Issue: The webhook maps PayPal's resource status onto `Refund.STATUS_COMPLETED`/`STATUS_FAILED` only because the external enum strings coincidentally match internal constants — fragile coupling with no named mapping.
Fix: Introduce an explicit `PAYPAL_REFUND_STATUS_MAP` translating PayPal status → Refund status.

[SEVERITY: MEDIUM]
File: apps/payments/paypal_disputes_service.py
Line: 396-401, 427-429, 514-515
Principle: No Magic Numbers/Strings
Issue: Bare status/action literals (`'SENT'`, `'EVIDENCE_SENT'`, `'ACCEPTED'`, `'DISPUTE_RESOLVED'`, `'NOTE_ADDED'`, `'MATCHED'`/`'RESOLVED_WON'`/`'RESOLVED_LOST'`) used even though the model-constant form exists elsewhere in the same file.
Fix: Replace with the corresponding `Dispute.STATUS_*`, `DisputeDocument.STATUS_*`, `DisputeActivityLog.ACTION_*` constants.

[SEVERITY: MEDIUM]
File: apps/payments/refund_service.py
Line: 31-41, 165-243, 658-698
Principle: Testability & Code Quality / DRY
Issue: `_process_paypal_refund`/`get_refund_status` import urllib/json inside the body and hand-roll HTTP with no shared client; `initiate_refund` ~100 lines; duplicates plumbing in woocommerce_service and paypal_disputes_service.
Fix: Extract one shared PayPal HTTP helper, move imports to module top, split the call/persist phases.

[SEVERITY: MEDIUM]
File: apps/payments/serializers.py
Line: 99-117
Principle: DRY
Issue: `RefundCreateSerializer.validate` re-implements the over-refund cap (reserved-sum vs price_paid) duplicated verbatim in `RefundService._reserve_refund` (398-408); two copies will drift.
Fix: Expose `RefundService.remaining_refundable(claim)` and call it from both.

[SEVERITY: MEDIUM]
File: apps/payments/document_service.py
Line: 742-784
Principle: Slow Queries / N+1
Issue: `_zendesk_comment_panels` performs synchronous network downloads inside a nested loop over every comment × attachment, so a ticket with many images blocks on dozens of serial HTTP round-trips during report generation.
Fix: Collect all (comment, url) pairs and fetch concurrently (bounded ThreadPoolExecutor) or cache by content_url; document the `max_images=14` bound.

[SEVERITY: MEDIUM]
File: apps/payments/paypal_disputes_service.py
Line: 359, 480, 571
Principle: DRY
Issue: Three near-identical `try: Dispute.objects.get(...) except DoesNotExist: log+return False` blocks across `provide_evidence`/`accept_claim`/`send_message`.
Fix: Extract `_get_local_dispute(dispute_id) -> Optional[Dispute]`.

[SEVERITY: MEDIUM]
File: apps/payments/document_service.py
Line: 1192-1269
Principle: Testability & Code Quality
Issue: `build_dispute_evidence_bundle` is ~78 lines (fetch, assembly, AI narration, identity cross-check, consent, asset loading) returning an untyped bare dict.
Fix: Split the build-items/narrate/group middle block into `_build_sections(...)`; consider a TypedDict for the returned context.

[SEVERITY: LOW]
File: apps/payments/views.py
Line: 49, 97
Principle: Security
Issue: `PayPalWebhookView` authenticates via `SystemSettings.sidebar_secret_token` (a secret shared with the Zendesk sidebar app) rather than a PayPal-specific webhook secret/signature.
Fix: Use a dedicated `paypal_webhook` secret or real PayPal signature verification via `paypal_webhook_id`.

[SEVERITY: LOW]
File: apps/payments/frontend_views.py
Line: 442-443, 466-468
Principle: Slow Queries / N+1
Issue: `dispute_detail` fetches without `select_related('claim')` and the activity_log renders `performed_by` → template N+1.
Fix: `select_related('claim')` on the dispute and `select_related('performed_by')` on activity_log.

[SEVERITY: LOW]
File: apps/payments/frontend_views.py
Line: 313-326
Principle: Slow Queries / N+1
Issue: `dispute_pull_from_paypal` loops over dispute_ids calling `ingest_dispute` one at a time (serial PayPal calls). Acceptable given low manual-trigger frequency.
Fix: Batch the PayPal listing/ingest if backfill volume grows. (Noted per false-positive-preferred.)

[SEVERITY: LOW]
File: apps/payments/frontend_views.py
Line: 156, 241, 380, 443, 448, 462
Principle: No Magic Numbers/Strings
Issue: Scattered literals: currency `[:3]`/default `'USD'`, log slice `[:50]`, evidence `[:10]`, claim-ref cap `[:6]`, paginator size 20.
Fix: Promote recurring ones (page size, default currency, display limits) to named constants.

[SEVERITY: LOW]
File: apps/payments/refund_service.py
Line: 58-66
Principle: Testability & Code Quality
Issue: Public service methods loosely typed for a money path (`Dict[str, Any]` returns, untyped `user`).
Fix: Add precise type hints / a TypedDict result.

[SEVERITY: LOW]
File: apps/payments/refund_service.py
Line: 312-314, 617
Principle: Separation of Concerns / Data Integrity
Issue: `process_webhook_refund` hardcodes `refund_type=Refund.TYPE_FULL` for every webhook-created refund, so partial PayPal refunds are mislabeled FULL in the audit trail.
Fix: Infer full/partial from amount vs `claim.price_paid` (as the WooCommerce path does) or store UNKNOWN.

[SEVERITY: LOW]
File: apps/payments/views.py
Line: 290-330
Principle: DRY
Issue: `process`/`issue`/`create` actions each repeat serializer-validate + extract + call-service + shape-response boilerplate.
Fix: Factor the validate-and-extract helper and a shared result→Response mapper.

[SEVERITY: LOW]
File: apps/payments/models.py
Line: 433-449, 466
Principle: Django Best Practices
Issue: `DisputeDocument` retains `DOC_TYPE_RESPONSE_LETTER` and `STATUS_REVIEW/ACCEPTED/SENT` choices the views comment as legacy/retired; dead choices invite misuse.
Fix: Remove with a migration or annotate as deprecated.

[SEVERITY: LOW]
File: apps/payments/models.py
Line: 295, 649, 652
Principle: Django Best Practices / Slow Queries
Issue: `raw_webhook_payload`/`paypal_response` JSON fields are filtered by JSON-key lookups in the dispute-list tabs with no GIN index.
Fix: If the dispute table grows, add a GIN index (Postgres) on the JSON fields backing the status filters.

[SEVERITY: LOW]
File: apps/payments/models.py
Line: 290
Principle: Django Best Practices
Issue: `JSONField(default=dict)` callable defaults are correct, but the payload fields are read via chained `.get()` everywhere with no schema/validation.
Fix: Document the expected payload shape or add light validation at write time.

[SEVERITY: LOW]
File: apps/payments/frontend_views.py
Line: 270-274, 457-461
Principle: DRY
Issue: The `SystemSettings.get_instance().zd_subdomain` try/except lookup is copy-pasted in `dispute_list` and `dispute_detail`.
Fix: Extract `_zd_subdomain()`.

[SEVERITY: LOW]
File: apps/payments/document_service.py
Line: 153-1187 (many)
Principle: Testability & Code Quality
Issue: Many public/module-level functions lack full type hints (untyped `dispute`/`claim`/`comments`).
Fix: Add `Dispute`/`Claim` hints and concrete return types.

[SEVERITY: LOW]
File: apps/payments/document_service.py
Line: 954
Principle: Testability & Code Quality
Issue: `_bottom_line(dispute, identity: dict, consent: dict = None)` annotates `dict` but defaults `None`.
Fix: `consent: Optional[dict] = None`.

[SEVERITY: LOW]
File: apps/payments/document_service.py
Line: 32-45, 207-211
Principle: Django Best Practices
Issue: WeasyPrint is re-imported on every PDF render via `_get_weasyprint`.
Fix: Resolve the optional import once at module load.

[SEVERITY: LOW]
File: apps/payments/document_service.py
Line: 48-74
Principle: DRY
Issue: `_fetch_zendesk_ticket_full` re-imports and shadows the same-named function already imported at module top.
Fix: Drop the redundant inner import or rename the wrapper.

[SEVERITY: LOW]
File: apps/payments/management/commands/generate_dispute_report.py
Line: 89-90
Principle: Separation of Concerns
Issue: The command calls the service's private `docsvc._render_to_pdf`.
Fix: Expose a public `render_bundle_to_pdf(...)`.

[SEVERITY: LOW]
File: apps/payments/paypal_disputes_service.py
Line: 19
Principle: Testability & Code Quality
Issue: `import socket` is unused.
Fix: Remove it.

[SEVERITY: LOW]
File: apps/payments/paypal_disputes_service.py
Line: 113-190
Principle: Security
Issue: The token endpoint's HTTPError path logs the full `error_body` uncapped, unlike `_post_dispute_action_multipart` which caps to `[:1000]`.
Fix: Length-cap/sanitize the token-path error-body log.

[SEVERITY: LOW]
File: apps/payments/paypal_disputes_service.py
Line: 433, 519, 608
Principle: Security
Issue: Activity-log `details` truncation is inconsistent (200 chars vs length-only vs full doc-id list) — low risk inside the trust zone.
Fix: Standardize what goes into `details`.

[SEVERITY: LOW]
File: apps/payments/utils.py
Line: 159-160
Principle: Separation of Concerns
Issue: `_gather_evidence_images` infers MIME from `split('.')[-1]` defaulting to image/jpeg — a mis-named file embeds with a forced MIME (low impact, internal PDF embed).
Fix: Optionally sniff magic bytes instead of trusting the extension.

---

## integrations

[SEVERITY: MEDIUM]
File: apps/integrations/views.py
Line: 993-1083
Principle: Thin Views / Fat Services
Issue: `_handle_status_change` is a ~90-line view method carrying the entire status-mirror workflow (status resolution, atomic claim+timeline write, client-update drafting, follow-up scheduling, cadence cancellation, AI summary back-fill).
Fix: Move into `services.mirror_claim_status_change(claim, custom_status_id)` returning a result; the view maps it to a Response.

[SEVERITY: MEDIUM]
File: apps/integrations/views.py
Line: 1051-1073
Principle: Separation of Concerns / Layering
Issue: The client-update side-effect block (draft initial message, schedule first follow-up, cancel cadence on close) is full business logic in the webhook view, wrapped in a broad `try/except Exception` that swallows everything.
Fix: Extract `handle_client_update_on_status_change(...)`; keep the view a dispatcher.

[SEVERITY: MEDIUM]
File: apps/integrations/views.py
Line: 1104-1189
Principle: Thin Views / Fat Services
Issue: `ZendeskFlightLookupView.post` is ~85 lines orchestrating claim/claimless branching, provider calls, normalization, AI analysis, atomic write + timeline, and note posting.
Fix: Delegate to a `run_flight_lookup(ticket_id, refresh)` service.

[SEVERITY: MEDIUM]
File: apps/integrations/views.py
Line: 1466-1513
Principle: Thin Views / Fat Services
Issue: `ZendeskClientUpdatesView._act` regenerates drafts, posts public Zendesk replies, sets `client_report_sent_at`, multiple `claim.save()` — business logic in the view.
Fix: Move each action into `communications/client_updates.py` service functions.

[SEVERITY: MEDIUM]
File: apps/integrations/views.py
Line: 762-858
Principle: Idempotency
Issue: `RefundWebhookView.post` posts to Zendesk guarded only by `result.get('already_processed')`; the dedup lives in a unique constraint with no visible transaction boundary, so a concurrent duplicate could double-post the comment.
Fix: Make `process_woocommerce_refund` resolve `already_processed` atomically (select_for_update/get_or_create); document the contract at the call site.

[SEVERITY: MEDIUM]
File: apps/integrations/views.py
Line: 1075-1080
Principle: Atomicity & Data Integrity
Issue: After the atomic status write, `refresh_claim_summary` (persists `ai_summary`) and `entry.save()` run outside any transaction; a crash between them leaves the timeline entry without its summary, and the summary write is unguarded.
Fix: Wrap the `entry.llm_summary` back-fill + summary persist in a short `transaction.atomic()`; comment that the network fetches are deliberately outside.

[SEVERITY: MEDIUM]
File: apps/integrations/services.py
Line: 84-118 (and ~10 call sites)
Principle: DRY
Issue: `_get_zendesk_auth_headers`/`_get_zendesk_base_url` each call `SystemSettings.get_instance()`, and every public function re-derives base_url/headers/timeout inline.
Fix: Introduce `_zendesk_request(method, path, *, params, body)` that builds URL/headers/timeout, opens the connection, returns parsed JSON.

[SEVERITY: MEDIUM]
File: apps/integrations/services.py
Line: 171-186, 263-278, 354-365, 403-414, 709-720, 791-802, 854-865
Principle: DRY
Issue: The four-branch except ladder (HTTPError/URLError/ValueError/Exception) with near-identical logging is copy-pasted into every Zendesk call.
Fix: Centralize in the shared request helper above.

[SEVERITY: MEDIUM]
File: apps/integrations/services.py
Line: 165, 217, 293, 328, 390, 428, 696, 783, 831, 970
Principle: No Magic Numbers/Strings
Issue: `getattr(settings, 'ZENDESK_TIMEOUT', 30)` repeats the literal `30` at every call site.
Fix: `ZENDESK_DEFAULT_TIMEOUT = 30` module constant (or fold into the request helper).

[SEVERITY: MEDIUM]
File: apps/integrations/services.py
Line: 462-623
Principle: Testability & Code Quality
Issue: `create_claim_from_zendesk_ticket` is ~160 lines mixing fetch IO, the form-ticket gate, LLM extraction, email/status resolution, DB write, AI-summary backfill.
Fix: Extract `_resolve_client_email`, `_resolve_status`, `_build_claim_kwargs`; keep the top function as orchestration.

[SEVERITY: MEDIUM]
File: apps/integrations/flight_lookup.py
Line: 241-254
Principle: Idempotency
Issue: `_aerodatabox_get`'s `for attempt in (1, 2)` retry can fall through with no return on unexpected control flow, and the rate-limit retry is a blocking `time.sleep` on the request thread that can fire repeatedly per click.
Fix: Iterate `range(MAX_ATTEMPTS)` with a guaranteed return/raise; cap total sleep per click or move retries off the sync path.

[SEVERITY: MEDIUM]
File: apps/integrations/flight_lookup.py
Line: 37-43, 241
Principle: No Magic Numbers/Strings
Issue: Provider constants exist (`AERODATABOX_TIMEOUT`, `RATE_LIMIT_RETRY_PAUSE`) but the attempt count `(1, 2)` is an inline literal.
Fix: Add `MAX_PROVIDER_ATTEMPTS = 2` and drive the loop from it.

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 170, 369, 503, 588, 701, 784, 890, 1102, 1342, 1409, 1447
Principle: DRY
Issue: `permission_classes = [AllowAny]` + manual `ZendeskSidebarAuth.reject_if_unauthenticated(...)` copy-pasted across all 11 views.
Fix: Implement a DRF authentication/permission class (or base APIView) so each view declares it once.

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 793-800, 899-906
Principle: DRY
Issue: The `X-Webhook-Secret` constant-time comparison is duplicated between `RefundWebhookView` and `ZendeskClaimWebhookView`.
Fix: Extract `verify_webhook_secret(request) -> Response | None`.

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 174, 197, 205, 406, 551, 617, 1115, 1357, 1421, 1455
Principle: Slow Queries / N+1
Issue: `Claim.objects.filter(zd_ticket_id=...)`/`filter(client_email=...).first()` is repeated in nearly every view and relies on those fields being indexed.
Fix: Verify `Claim.zd_ticket_id` and `Claim.client_email` carry `db_index=True`; add if missing.

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 1031-1035, 1167-1179, 1232-1241, 1282-1290, 1299-1307
Principle: No Magic Numbers/Strings
Issue: Timeline `update_type` values (`'STATUS_CHANGE'`, `'INFO_UPDATED'`) and `'flight_lookup'` key are bare literals scattered across methods.
Fix: Reference `ClaimUpdateTimeline.TYPE_*` constants.

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 655, 659, 815, 1260-1261, 1433
Principle: No Magic Numbers/Strings
Issue: Bare business literals: default currency `'USD'`, 14-day history window, history `[-10:]`/`[:1000]`, email page size `[:50]`.
Fix: Named constants (`DEFAULT_CURRENCY`, `FLIGHT_HISTORY_WINDOW_DAYS = 14`, `CHAT_HISTORY_TURNS`, `EMAILS_PAGE_SIZE`).

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 28-55, 395-399, 535-539, 606-607, 719, 942, 1061-1062, 1345-1346, 1479, 1488
Principle: Django Best Practices
Issue: Heavy reliance on function-local imports scattered through view bodies.
Fix: Hoist to module level unless breaking a documented cycle.

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 216, 230, 754, 853, 986
Principle: Testability & Code Quality
Issue: Broad `except Exception` wraps whole get/post bodies, masking the failure point and catching programming errors as 500s.
Fix: Narrow the try-scope to the specific I/O; log with the operation name.

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 237, 272, 306, 337, 993, 1191, 1248, 1466, 1515
Principle: Testability & Code Quality
Issue: View helper methods lack type hints on params/returns.
Fix: Add `claim: Claim`, `-> dict`, `-> Response` hints.

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 177, 201, 207, 210, 217, 231, 404, 549, 574, 685
Principle: Django Best Practices
Issue: Pervasive f-string logging evaluates eagerly and bypasses lazy `%`-formatting used elsewhere in the same file.
Fix: Use `logger.info("... %s ...", var)`.

[SEVERITY: LOW]
File: apps/integrations/views.py
Line: 1-1539 (module)
Principle: Separation of Concerns / Layering
Issue: One `views.py` holds 11 unrelated endpoint classes (~1500 lines) mixing the public webhook surface with sidebar UI helpers.
Fix: Split into `views/webhooks.py`, `views/sidebar.py`, `views/flight.py`, `views/client_updates.py`.

[SEVERITY: LOW]
File: apps/integrations/services.py
Line: 493-495, 600-608
Principle: Idempotency
Issue: The pre-check + IntegrityError recovery for claim creation relies on a `zd_ticket_id` unique constraint asserted only in the docstring.
Fix: Confirm `Claim.zd_ticket_id` is `unique=True`; add a regression test.

[SEVERITY: LOW]
File: apps/integrations/services.py
Line: 614-621
Principle: Atomicity & Data Integrity
Issue: `refresh_claim_summary` runs after the create `transaction.atomic()` closes and itself saves; correct as best-effort but undocumented.
Fix: Document that creation commits before the summary attempt so a summary failure never rolls back the claim.

[SEVERITY: LOW]
File: apps/integrations/services.py
Line: 759, 1124, 1365-1382
Principle: No Magic Numbers/Strings
Issue: Bare caps: search-query `1000` (×2), LLM context `comments[:5]`, thread `[:30]`/`[:200]`/`[:3000]`/`[:1500]`.
Fix: Named constants (`MAX_SEARCH_QUERY_LEN`, `LLM_CONTEXT_MAX_COMMENTS`, `THREAD_*`).

[SEVERITY: LOW]
File: apps/integrations/services.py
Line: 1026, 1050-1056
Principle: No Magic Numbers/Strings
Issue: The refund-comment template and `"refunded"` tag string are hardcoded inline in the service.
Fix: Move to module constants / a templates module.

[SEVERITY: LOW]
File: apps/integrations/services.py
Line: 24, 34, 1129, 1279, 1330, 1356
Principle: Testability & Code Quality
Issue: Several functions lack hints (`safe_date`/`safe_decimal`, `build_claim_facts`, `build_ticket_thread`, `create_claim_from_zendesk_ticket` params).
Fix: Add type hints and TypedDicts for the extraction/facts return shapes.

[SEVERITY: LOW]
File: apps/integrations/services.py
Line: 184-186, 276-278, 300-302, 412-414, 718-720, 800-802, 974-976, 1004-1006, 1060-1062
Principle: Django Best Practices
Issue: Many handlers catch bare `except Exception` returning None/[]/False, most without `exc_info=True`.
Fix: Narrow exceptions where feasible; keep `exc_info=True`.

[SEVERITY: LOW]
File: apps/integrations/services.py
Line: 1323-1325
Principle: Slow Queries / N+1
Issue: `build_claim_facts` issues three `.count()`/`.filter().count()` off `claim.emails` plus a Dispute count; `emails = claim.emails.all()` is assigned but never reused.
Fix: Annotate counts upstream; drop the unused binding. (N+1 only if called per-row.)

[SEVERITY: LOW]
File: apps/integrations/flight_lookup.py
Line: 32-33
Principle: Separation of Concerns / Layering
Issue: services ↔ briefing cycle is worked around by a lazy in-function import (`briefing.refresh_claim_summary` at services.py:617).
Fix: Extract shared `build_claim_facts`/business-context into a lower-level module both depend on.

[SEVERITY: LOW]
File: apps/integrations/flight_lookup.py
Line: 416-460, 544-579, 565-568
Principle: Testability & Code Quality / Django Best Practices
Issue: `analyze_flight_match`/note formatters use untyped `analysis`/`verdict`; `format_flight_note` (~36 lines) indexes legs with bare subscripts (KeyError on malformed leg).
Fix: Add type hints, extract a `_leg_line(...)` helper, use `leg.get(...)`.

[SEVERITY: LOW]
File: apps/integrations/briefing.py
Line: 64, 91, 95-104
Principle: No Magic Numbers/Strings / Testability
Issue: `[-30:]` comment cap duplicated across modules; `temperature=0.4`/`max_tokens=500` inline; `normalize_fetched_comments` untyped.
Fix: Shared `MAX_THREAD_COMMENTS` constant; named AI-tuning constants; add type hints.

---

## communications

[SEVERITY: HIGH]
File: apps/communications/client_updates.py
Line: 369-390
Principle: Atomicity & Data Integrity
Issue: `send_follow_up` posts the public Zendesk reply BEFORE writing SENT state, and the state write is not ordered via `transaction.on_commit`, so a save() failure after a successful post duplicates the client reply on the next run (code marks this "ACCEPTED RISK").
Fix: Flip state to SENT under `select_for_update` inside `transaction.atomic()` and move the external post into `transaction.on_commit()`, or persist a "send in progress" marker before posting so a retry detects the prior attempt.

[SEVERITY: MEDIUM]
File: apps/communications/services.py
Line: 389-514
Principle: Testability & Code Quality
Issue: `parse_ai_response` is ~125 lines with a near-duplicate category-inference block in the main body (449-464) and the except fallback (499-507).
Fix: Extract `_infer_category_from_text(raw_lower)` and `_extract_*` helpers; call from both paths.

[SEVERITY: MEDIUM]
File: apps/communications/services.py
Line: 444-447
Principle: DRY / No Magic Strings
Issue: The valid-categories list is hardcoded, duplicating `EmailLog.CATEGORY_CHOICES`.
Fix: Derive from `[c[0] for c in EmailLog.CATEGORY_CHOICES]`.

[SEVERITY: MEDIUM]
File: apps/communications/services.py
Line: 45-58, 718, 1009-1015, 1049-1050
Principle: DRY / No Magic Strings
Issue: Category strings hardcoded in `AUTO_RESOLVABLE_CATEGORIES`, `AI_TAG_BY_CATEGORY`, and the auto_resolve branch instead of `EmailLog.CATEGORY_*` constants.
Fix: Reference the model constants so a rename is a NameError.

[SEVERITY: MEDIUM]
File: apps/communications/client_updates.py
Line: 70-74, 206, 214, 217, 223, 250, 308
Principle: DRY / No Magic Strings / Separation of Concerns
Issue: Filters use bare `category='OBJECT_FOUND'`, refund `status='COMPLETED'`, claim `status_category='solved'`, and bare-string `CLIENT_SAFE_REPLY_CATEGORIES`.
Fix: Reference `EmailLog.CATEGORY_*` and the refund/claim status constants.

[SEVERITY: MEDIUM]
File: apps/communications/client_updates.py
Line: 244
Principle: No Magic Numbers
Issue: `_since_anchor` falls back to `now() - timedelta(days=30)` — an undocumented bare 30-day lookback.
Fix: Named constant (reuse `DEFAULT_SERVICE_LENGTH_DAYS` or add `SINCE_ANCHOR_FALLBACK_DAYS`).

[SEVERITY: MEDIUM]
File: apps/communications/services.py
Line: 1173-1183
Principle: Slow Queries / N+1 / Separation of Concerns
Issue: `check_email_for_ticket` does two synchronous Zendesk round-trips plus a summary refresh inline at the end of an HTTP button handler.
Fix: Move the post-ingestion summary refresh to `transaction.on_commit` or a deferred job.

[SEVERITY: MEDIUM]
File: apps/communications/services.py
Line: 866, 979
Principle: No Magic Numbers
Issue: IMAP timeout default `30` is a bare literal in two places.
Fix: `DEFAULT_IMAP_TIMEOUT` in constants.py.

[SEVERITY: MEDIUM]
File: apps/communications/serializers.py
Line: 9-10
Principle: Slow Queries / N+1
Issue: The serializer sources `claim_id`/`claim_status` from the related Claim, so any list endpoint without `select_related('claim')` N+1s; the one viewset remembers to, but the coupling is hidden.
Fix: Use `source='claim_id'` for the id (no join) and document the select_related dependency.

[SEVERITY: LOW]
File: apps/communications/views.py
Line: 16-33
Principle: Django Best Practices
Issue: Class docstring says "Read-only for AGENT and MANAGER" and references a long-gone `sentiment` field; role split removed.
Fix: Update the prose to single-user-type (keep the intentional `IsAgentOrManager` alias).

[SEVERITY: LOW]
File: apps/communications/views.py
Line: 72-73
Principle: Django Best Practices / Separation of Concerns
Issue: `BooleanField` imported inside `resolve()` and the boolean parsed ad hoc in the view.
Fix: Module-level import; parse via a small DRF serializer.

[SEVERITY: LOW]
File: apps/communications/services.py
Line: 130, 283-285, 297-299, 804-807
Principle: Django Best Practices
Issue: Multiple bare `except Exception` in the email pipeline mask programming errors.
Fix: Narrow to expected types (UnicodeDecodeError, imaplib.IMAP4.error); keep catch-all only at the per-email boundary.

[SEVERITY: LOW]
File: apps/communications/services.py
Line: 33, 41-58
Principle: No Magic Numbers / Separation of Concerns
Issue: `MAX_EMAILS_PER_RUN = 20`, `IMAP_MONTHS`, category/tag maps live in services.py rather than the project's central constants.py.
Fix: Move the tunables to constants.py.

[SEVERITY: LOW]
File: apps/communications/services.py
Line: 314, 354, 530, 590
Principle: Testability & Code Quality
Issue: Incomplete type hints on public service functions (`call_qwen_ai known_pii=None`, `_known_pii_for_email`, `post_ai_summary_to_zendesk`).
Fix: Add explicit hints (`known_pii: Optional[dict] = None`, `claim: Optional[Claim]`).

[SEVERITY: LOW]
File: apps/communications/services.py
Line: 789-797
Principle: Idempotency
Issue: In the validation-error fallback, `EmailLog.objects.create` can run with `message_id=''`, and blank ids bypass the unique dedup, so a re-run could create a duplicate review row.
Fix: Skip creation (or log explicitly) when message_id is blank in this path.

[SEVERITY: LOW]
File: apps/communications/services.py
Line: 148
Principle: Django Best Practices
Issue: HTML→text fallback uses `re.sub(r'<[^>]+>', '', body_html)` — doesn't decode entities or strip `<style>/<script>`, so AI-fed/stored body carries entity noise/CSS.
Fix: `html.unescape` after dropping script/style blocks.

[SEVERITY: LOW]
File: apps/communications/client_updates.py
Line: 79-86, 215-227
Principle: Django Best Practices
Issue: Broad `try/except Exception: pass`/return-default around SystemSettings/refunds/Dispute import in the cadence safety gate silently swallows real DB errors.
Fix: Catch specific expected exceptions (ImportError for optional Dispute, AttributeError); let DB errors surface/log.

[SEVERITY: LOW]
File: apps/communications/client_updates.py
Line: 442-469
Principle: Slow Queries / N+1
Issue: `run_due_updates` re-fetches each update with a fresh `.get(pk=...).select_related('claim')` and issues per-claim refund/dispute/follow-up queries in the loop.
Fix: Reuse the already-selected claim; batch-load related rows if the due queue can grow.

---

## users

[SEVERITY: HIGH]
File: apps/users/views.py
Line: 118-121
Principle: Security
Issue: `logout_view` has no `@require_POST` and logs out on any GET, so it's logout-CSRF (verified). Real-world impact is low (single-user internal tool; effect is just an unwanted logout) — reviewer would rate MEDIUM.
Fix: Add `@require_POST` (the logout form already POSTs); optionally `@login_required`.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 430-459
Principle: Thin Views / Fat Services
Issue: `agent_assign_claim` holds assignment logic inline (parse, lookup, assign/unassign branch, write).
Fix: Move to `claims.services.assign_claim(claim, agent_id)`.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 462-552
Principle: Thin Views / Fat Services
Issue: `agent_upload_evidence` is ~90 lines doing the entire file-validation pipeline (size, extension, libmagic, filetype, temp-file, sanitization, create).
Fix: Move to a Form `clean_image()` or `validate_and_store_evidence(...)` service.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 472-549
Principle: No Magic Numbers/Strings
Issue: `max_size = 10*1024*1024`, `allowed_extensions`, `allowed_mime_types`, the `1024`-byte sniff read are bare literals in the view.
Fix: Constants in `users/constants.py` (`EVIDENCE_MAX_BYTES`, `EVIDENCE_ALLOWED_*`, `MAGIC_SNIFF_BYTES`).

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 660-712
Principle: Testability & Code Quality
Issue: `manager_dashboard` is ~90 lines of claim/email/dispute stat assembly in one HTTP handler.
Fix: Extract `build_dashboard_stats()` service returning the context dict.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 156-171, 677-692
Principle: DRY
Issue: Identical `email_stats` and `email_category_stats` aggregates copy-pasted between `agent_dashboard` and `manager_dashboard`.
Fix: Extract `email_overview_stats()`.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 698-704, 909, 937-940
Principle: No Magic Numbers/Strings
Issue: Dispute status strings, `'Refund Requested'`, and `'COMPLETED'/'PENDING'/'FAILED'` are bare literals duplicating model choices.
Fix: Reference `Dispute`/`Refund` status constants.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 982-995
Principle: Atomicity & Data Integrity
Issue: `manager_settings` does `form.save()` then sets sensitive fields and `settings.save()` again with no `transaction.atomic()`.
Fix: Wrap the two writes in `transaction.atomic()`.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 966-1035
Principle: Thin Views / Fat Services
Issue: `manager_settings` mixes HTTP with business logic (service-status get_or_create loop, sensitive-field persistence, Zendesk custom-status fetch/cache).
Fix: Move seeding and custom-status fetch/cache into services.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 1038-1087
Principle: Thin Views / Fat Services
Issue: `manager_users` does user-creation logic inline (uniqueness check, password validation, atomic create) instead of a UserCreationForm.
Fix: Use a `UserCreationForm` subclass; the view calls `form.save()`.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 1059-1062
Principle: Idempotency / Atomicity & Data Integrity
Issue: User creation guards uniqueness with a non-atomic `.exists()` then `create_user` (read-modify-write race), relying on the constraint only via a broad except.
Fix: Catch `IntegrityError` specifically (or use a form `clean_username`).

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 99-102
Principle: Django Best Practices
Issue: `login_view` reads username/password from `request.POST` and validates manually instead of `AuthenticationForm`.
Fix: Use `AuthenticationForm` / Django's `LoginView`.

[SEVERITY: MEDIUM]
File: apps/users/views.py
Line: 608, 640, 827, 949, 972, 1015
Principle: Slow Queries / N+1
Issue: `SystemSettings.get_instance()` runs `get_or_create(pk=1)` on every request across many views with no caching.
Fix: Cache the singleton (`cache.get_or_set`) and invalidate on save in `manager_settings`.

[SEVERITY: MEDIUM]
File: apps/users/constants.py
Line: 5-6
Principle: Security (rate limiting)
Issue: Login throttle is IP-only, 60s window, max 5, cleared on success — weak brute-force protection and collapses all office-NAT users into one bucket. (Low-confidence given internal-only scope.)
Fix: Key by username+IP, lengthen window / add lockout; or document the threat model.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 246-273 + many
Principle: Django Best Practices / Separation of Concerns
Issue: Pervasive function-local imports scattered through view bodies.
Fix: Hoist to module level unless breaking a real cycle.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 246-273
Principle: Separation of Concerns / Layering
Issue: `_annotate_deadline` mutates the Claim instance with presentation fields and hardcodes `0`/`7`/`14`-day thresholds in the view.
Fix: Move to a presenter/template tag; name the thresholds.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 231, 603, 816, 824, 945
Principle: No Magic Numbers/Strings
Issue: "stuck" threshold `> 14` days and page-size `20` repeated across views.
Fix: `CLAIM_STUCK_DAYS`, `LIST_PAGE_SIZE` constants.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 936
Principle: Slow Queries / N+1
Issue: Refund stats issue 5 separate `.count()`/aggregate queries on the same queryset.
Fix: Combine into one `.aggregate()` with `Case/When`.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 47-53
Principle: Slow Queries
Issue: `_claim_status_choices()` runs DISTINCT+ORDER BY over the whole Claim table on every list render.
Fix: Cache briefly or derive from an enum.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 61
Principle: Testability & Code Quality
Issue: Helpers lack type hints (`rate_limit_logins`, `_annotate_deadline`, `_claim_status_choices`, `_zendesk_ticket_base`, `_followup_and_claim`).
Fix: Add hints.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 142-144, 196-243
Principle: Testability & Code Quality
Issue: `agent_claims`/`agent_dashboard` docstrings & comments still describe per-agent assignment semantics the single-user model no longer enforces.
Fix: Update docs; keep `assigned_to` only if used as a soft label.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 1069
Principle: Django Best Practices
Issue: Weak-password branch re-renders `users.html` dropping submitted field values (form wiped on error).
Fix: Use a bound form re-rendered with values + errors.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 322-329
Principle: Separation of Concerns / DRY
Issue: `_followup_and_claim` is a trivial pass-through since the assignment guard was removed.
Fix: Inline `get_object_or_404` into callers or simplify.

[SEVERITY: LOW]
File: apps/users/views.py
Line: 351-355
Principle: Django Best Practices
Issue: Emptiness is checked on the stripped body but the unstripped `request.POST.get('body')` is sent.
Fix: `body = (request.POST.get('body') or '').strip()` once, pass that.

---

## config

[SEVERITY: HIGH]
File: apps/config/api/views.py
Line: 119
Principle: Atomicity & Data Integrity
Issue: `toggle_setting_flag` saves the in-memory `SystemSettings` singleton (loaded via `from_db_value`, possibly holding a DECRYPTION_FAILED sentinel) and races the settings-form view / connection tester, so a concurrent Save clobbers the flag (lost update).
Fix: Use `SystemSettings.objects.filter(pk=1).update(**{flag: enabled})` for an atomic single-column write; refresh/return.

[SEVERITY: MEDIUM]
File: apps/config/api/views.py
Line: 84-89
Principle: Atomicity & Data Integrity
Issue: `toggle_service` is an unguarded read-modify-write (`get_object_or_404` → mutate → full `.save()`) that races the connection tester's `update_or_create` and rewrites every column from a stale copy.
Fix: `ServiceStatus.objects.filter(service=service).update(is_enabled=...)` (or `save(update_fields=['is_enabled'])`).

[SEVERITY: MEDIUM]
File: apps/config/models.py
Line: 375-388
Principle: Atomicity & Data Integrity
Issue: Singleton `save()` forces `pk=1` and `get_instance()` uses `get_or_create(pk=1)` with no atomic/locking, so concurrent first-access can INSERT-race or overwrite a concurrently-saved instance.
Fix: Wrap fetch/update in `transaction.atomic()` + `select_for_update`, or use targeted `.update()` for single fields.

[SEVERITY: MEDIUM]
File: apps/config/services/connection_tester.py
Line: 39-42, 292
Principle: Security
Issue: `test_ai`/`test_woocommerce` issue `requests.get/post` against operator-supplied free-text URLs (`ai_api_base`, `woocommerce_store_url`) with no scheme/host validation — SSRF shape.
Fix: Require https and reject internal/loopback hosts (or allowlist) before probing. (Lower-risk given single trusted user, still worth hardening.)

[SEVERITY: MEDIUM]
File: apps/config/models.py
Line: 50-53
Principle: Django Best Practices
Issue: `ServiceStatus.last_checked` uses `auto_now_add=True`, so it's set only at creation; the heartbeat works only because writers set it manually — misleading semantics that will silently report stale heartbeats if a future save forgets.
Fix: Drop `auto_now_add`; set it explicitly (already done in writers) or rename to signal the manual contract.

[SEVERITY: MEDIUM]
File: apps/config/services/connection_tester.py
Line: 318-327
Principle: DRY
Issue: `test_all_services` omits `WOOCOMMERCE` though `test_woocommerce` exists; the service list is duplicated across model SERVICE_CHOICES, views `test_methods`, and this dict, and has drifted.
Fix: Drive all three from one canonical service-key → tester-method mapping.

[SEVERITY: LOW]
File: apps/config/services/scheduler_controller.py
Line: 23-29
Principle: Atomicity & Data Integrity
Issue: `toggle_enabled` does `get_or_create` → mutate → full `.save()`, racing the cron dispatcher's save of the same SCHEDULER row.
Fix: `save(update_fields=['is_enabled'])` or a filtered `.update()`.

[SEVERITY: LOW]
File: apps/config/services/connection_tester.py
Line: 24-65
Principle: Testability & Code Quality
Issue: Each `test_*` repeats ~40 lines of try/except + `_update_status` boilerplate with broad `except Exception`.
Fix: Table-driven probes via one helper taking (service, credential-check, probe-callable).

[SEVERITY: LOW]
File: apps/config/encrypted_fields.py
Line: 40
Principle: Testability & Code Quality
Issue: `_derive_fernet` is `@lru_cache(maxsize=16)` keyed on the raw secret, so key material persists in process memory and the cache can mask key changes outside override_settings.
Fix: Document the lifetime or expose a cache-clear hook for tests/rotation.

[SEVERITY: LOW]
File: apps/config/encrypted_fields.py
Line: 138
Principle: No Magic Numbers/Strings
Issue: Ciphertext inflation `max_length * 4 + 100` uses bare literals; `pii_tokenization_salt max_length=4580` is an unexplained magic number.
Fix: `_CIPHERTEXT_LENGTH_MULTIPLIER`/`_CIPHERTEXT_LENGTH_PADDING` constants; document 4580.

[SEVERITY: LOW]
File: apps/config/api/views.py
Line: 46-47, 72-74, 108-111
Principle: Separation of Concerns / Layering
Issue: Docstrings say "Manager-only"/"privileged" but `IsManager` is now an alias of `IsAuthenticated` (role split removed) — stale and misleading.
Fix: Update comments to "authenticated-only (role split removed)".

[SEVERITY: LOW]
File: apps/config/models.py
Line: 390-400
Principle: DRY
Issue: `SystemSettings.get_masked_value` is referenced only by tests — effective dead code masquerading as shared API.
Fix: Wire it into the settings-page masking or remove it.

[SEVERITY: LOW]
File: apps/config/models.py
Line: 4
Principle: DRY
Issue: `EncryptedTextField` imported but never used by any field.
Fix: Drop the unused import.

[SEVERITY: LOW]
File: apps/config/models.py
Line: 43
Principle: No Magic Numbers/Strings
Issue: `status` field `default='disconnected'` uses a bare literal though `STATUS_DISCONNECTED` is defined two lines below.
Fix: `default=STATUS_DISCONNECTED`.

[SEVERITY: LOW]
File: apps/config/admin.py
Line: 76-82
Principle: Testability & Code Quality
Issue: `enable_services`/`disable_services` admin actions and model methods (`mark_connected`, `mark_error`) lack type hints/docstrings.
Fix: Add hints and brief docstrings.

---

## core

[SEVERITY: HIGH]
File: apps/core/management/commands/run_scheduled_jobs.py
Line: 92-106
Principle: Idempotency
Issue: The dispatcher has no run-lock/dedup (verified); an overlapping cron tick or a manual `--job` run while a slow run executes runs every job concurrently, and `run_due_updates` then sends public replies twice while both runs race on the single SCHEDULER ServiceStatus row.
Fix: Acquire a non-blocking advisory lock (`pg_try_advisory_lock` or a `running_since` guard via `select_for_update(skip_locked=True)`) and bail if held; document if cron overlap is truly impossible.

[SEVERITY: MEDIUM]
File: apps/core/management/commands/run_scheduled_jobs.py
Line: 93-106
Principle: Idempotency
Issue: Jobs run (external side effects) BEFORE the heartbeat/state is written; a kill between a job's external call and the status write leaves the run unrecorded, with no per-job "already-ran-this-window" guard.
Fix: Each job owns its idempotency (dedup/last-run marker); write a "started" heartbeat before the loop and "finished" after.

[SEVERITY: LOW]
File: apps/core/management/commands/seed_test_data.py
Line: 31-72
Principle: Testability & Code Quality
Issue: `handle` is ~40 lines dominated by repetitive stdout writes, no type hints, and the broad `except Exception` re-raised as `CommandError` loses the original traceback.
Fix: Extract `_print_summary(counts)`; `raise CommandError(...) from e`. (Dev-only command.)

---

## claims

[SEVERITY: MEDIUM]
File: apps/claims/models.py
Line: 178-189
Principle: No Magic Numbers/Strings
Issue: `status` default `'Investigation initiated'` and `status_category` default `'open'` are bare literals duplicated across the model, the legacy map, and the webhook writer.
Fix: Module-level `DEFAULT_STATUS`/`DEFAULT_CATEGORY` constants referenced by the model and writers.

[SEVERITY: MEDIUM]
File: apps/claims/models.py
Line: 254-277
Principle: Slow Queries / N+1
Issue: Four `refund_*` properties each issue a separate query and `ClaimViewSet` does not prefetch `refunds`; `refund_status` calls `latest_refund`, doubling queries per row.
Fix: Prefetch `refunds` and/or expose via `annotate()` (Sum/Exists); have `refund_status` reuse `latest_refund`.

[SEVERITY: MEDIUM]
File: apps/claims/serializers.py
Line: 37-41
Principle: Slow Queries / N+1
Issue: `get_evidence_count` falls back to `obj.evidence.count()` (per-row query) whenever the `_evidence_count` annotation is absent — e.g. the detail/retrieve path.
Fix: Guarantee the annotation on every feeding queryset, or bind the field to the annotation source.

[SEVERITY: LOW]
File: apps/claims/views.py
Line: 121-150
Principle: Thin Views / Fat Services
Issue: `proof_of_work` does lazy imports, PDF generation, and HttpResponse/header assembly inline.
Fix: Move to `build_proof_of_work_response(claim)` (or bytes+filename) service.

[SEVERITY: LOW]
File: apps/claims/views.py
Line: 180-206
Principle: Thin Views / Fat Services
Issue: Evidence `perform_create` does lookup, not-found handling, image validation, and save with lazy imports in the view.
Fix: Extract `create_claim_evidence(claim_id, image, description)`.

[SEVERITY: LOW]
File: apps/claims/views.py
Line: 78-83, 145-150, 208-217
Principle: Django Best Practices
Issue: Three handlers catch bare `except Exception` returning a generic 500, masking programming errors.
Fix: Narrow the catches or let unexpected exceptions reach DRF's handler.

[SEVERITY: LOW]
File: apps/claims/views.py
Line: 30-49
Principle: Testability & Code Quality
Issue: `ClaimViewSet`/`ClaimEvidenceViewSet` docstrings still describe AGENT/MANAGER tiers, contradicting the removed role split.
Fix: Update to single authenticated user type.

[SEVERITY: LOW]
File: apps/claims/views.py
Line: 51-57
Principle: DRY
Issue: `ClaimEvidenceViewSet.get_queryset` re-implements a manual `claim` filter duplicating the declared `filterset_fields = ['claim']`.
Fix: Drop the manual filter; rely on DjangoFilterBackend.

[SEVERITY: LOW]
File: apps/claims/models.py
Line: 248-277
Principle: Separation of Concerns / Layering
Issue: `refund_total` does a function-local `Sum` import and hardcodes the cross-app status `'COMPLETED'` inside the Claim model to dodge a claims→payments cycle.
Fix: Move the aggregation to a payments service/manager, or reference the payments status constant via a late import so the coupling is explicit.

[SEVERITY: LOW]
File: apps/claims/serializers.py
Line: 43-45
Principle: Django Best Practices
Issue: `validate_client_email` lowercases the email, but the write paths that create claims from Zendesk/webhook don't, so normalization is entry-point-dependent and `client_email` has no uniqueness constraint.
Fix: Normalize in one place (model `save`/`clean` or a shared service).

---

## ai

[SEVERITY: MEDIUM]
File: apps/ai/client.py
Line: 108-109
Principle: No Magic Numbers/Strings
Issue: `temperature: float = 0.3` and `max_tokens: int = 600` are bare literal defaults in the public `complete()` signature.
Fix: `DEFAULT_TEMPERATURE`/`DEFAULT_MAX_TOKENS` module constants or `settings.AI_*`.

[SEVERITY: LOW]
File: apps/ai/client.py
Line: 145-146
Principle: DRY
Issue: `SystemSettings.get_instance()` is called here and again inside `_build_openai_client()` (line 83) on the same request path.
Fix: Fetch once and pass `ss` into `_build_openai_client(ss)`.

[SEVERITY: LOW]
File: apps/ai/client.py
Line: 41-46
Principle: Django Best Practices
Issue: `_resolve_salt` catches bare `except Exception` around `SystemSettings.get_instance()`, masking programming errors.
Fix: Narrow to `DatabaseError`/`SystemSettings.DoesNotExist`.

---

## agent

[SEVERITY: HIGH]
File: apps/agent/services.py
Line: 152-175
Principle: Security (LLM trust boundary)
Issue: The claim's `client_name` is never added to `known_pii` — only `client_email` goes into `aliases` (verified: `known_pii={"aliases": aliases}`, no `names` key) — and `RegexTokenizer` explicitly does not detect unknown names, so client names rendered into the trusted `claim_summary` / free-text reach the external LLM provider unredacted. (Two reviewer findings — lines 152-158 and 113-121 — are the same root cause.)
Fix: Collect `claim.client_name` (and alternates) into a `names` list and pass `known_pii={"aliases": aliases, "names": names}` so the tokenizer redacts them before the prompt leaves the trust boundary.

[SEVERITY: MEDIUM]
File: apps/agent/services.py
Line: 55-194
Principle: Testability & Code Quality
Issue: `process_message` is ~130 lines (claim-ID detection, history scan, context fetch, trusted/untrusted assembly, LLM call, error handling) — a God method.
Fix: Split into `_resolve_claim_ids`, `_build_trusted_payload`, `_build_untrusted_payload`, `_call_llm`.

[SEVERITY: MEDIUM]
File: apps/agent/services.py
Line: 317-407
Principle: Slow Queries / N+1
Issue: `fetch_context` loops over claim IDs issuing per-claim queries for emails/refunds/timeline plus a synchronous Zendesk HTTP call → O(N) DB + O(N) external calls, unbatched.
Fix: Batch related fetches with `claim__in` + prefetch; note the per-claim Zendesk call is blocking.

[SEVERITY: MEDIUM]
File: apps/agent/services.py
Line: 84, 135, 145, 338, 386, 395
Principle: No Magic Numbers/Strings
Issue: Bare literals drive context windows: `[-6:]`/`[-10:]`, `[:5]`, `[:500]`/`[:200]`, `[:10]`, and the `'ALF'` substring check.
Fix: Named constants (`HISTORY_LOOKBACK`, `MAX_EMAILS_IN_CONTEXT`, `EMAIL_BODY_CHARS`, ...).

[SEVERITY: MEDIUM]
File: apps/agent/services.py
Line: 188-189
Principle: Testability & Code Quality
Issue: f-string passed to `logger.error` instead of `%`-style lazy args, inconsistent with the rest of the module.
Fix: `logger.error("manager_chat: ... %s", e, exc_info=True)`.

[SEVERITY: LOW]
File: apps/agent/services.py
Line: 13-14, 273
Principle: DRY
Issue: `Q` imported at module top and re-imported inside `search_claims_by_name_or_email`; `QuerySet` imported but unused.
Fix: Remove the redundant/unused imports.

[SEVERITY: LOW]
File: apps/agent/services.py
Line: 425-493
Principle: Testability & Code Quality
Issue: `_handle_multiple_claims` and `_handle_no_claim_detected` are dead code (never called); the former references `context['count']`, a key `fetch_context` never produces.
Fix: Delete both, or wire them in and fix the missing key.

[SEVERITY: LOW]
File: apps/agent/services.py
Line: 91-93
Principle: Django Best Practices
Issue: `all_claim_ids` mixes normalized `ALF*` IDs with stringified integer PKs, but `fetch_context` only looks up by `alf_claim_id`, so integer-derived values silently become "not found".
Fix: Resolve numeric ids to `alf_claim_id` before merging, or look up by PK separately.

[SEVERITY: LOW]
File: apps/agent/services.py
Line: 216-260
Principle: Testability & Code Quality
Issue: `detect_name_or_email` uses a loose two-word regex + hardcoded keyword/stop-word lists — brittle, hard to test, easy to mis-trigger.
Fix: Extract the lists to constants; add unit tests; narrow the regex or gate on explicit cues.

[SEVERITY: LOW]
File: apps/agent/services.py
Line: 47, 55, 196, 262, 293
Principle: Testability & Code Quality
Issue: `__init__` and several public methods lack/mix type hints.
Fix: Add consistent hints (modern `list[...]`/`dict[...]`).

---

*Generated by a 9-agent parallel review. Headline HIGH findings re-verified
against source. Severity calibrated to single-trusted-user + LLM-only-PII-boundary design.*
