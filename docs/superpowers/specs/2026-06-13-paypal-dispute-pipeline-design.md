# PayPal Dispute Pipeline — Design Spec

**Date:** 2026-06-13
**Status:** Draft (design only — no implementation yet; awaiting user review + PayPal-side confirmations)
**Scope anchor:** LORA receives PayPal disputes, a human categorizes each one, LORA assembles a
category-specific evidence report (Zendesk data rendered as faithful Zendesk-styled panels → PDF), and
submits it back to PayPal before the deadline. LORA is the orchestrator and single source of truth; PayPal
and Zendesk are inside the trust zone; the LLM provider is the only outside party (all AI via
`apps/ai/AIClient`, PII-tokenized). No data is doubled — the Dispute, its documents, and the audit log are
the one store; the report is a *rendering* of fetched data, not a second copy.

---

## Context — what already exists (verified 2026-06-13)

The **manual workbench is built and largely works**; both ends of the **automation** are missing/broken.

Working: `Dispute` + `DisputeDocument` + screenshot + audit-log models (`apps/payments/models.py:158+`);
manager list/detail screens with an actions sidebar (`apps/payments/frontend_views.py`,
`templates/manager/dispute_detail.html`); the AI response-letter writer and the template evidence-report
PDF (`apps/payments/document_service.py:507-752`, WeasyPrint); the human edit/accept/delete loop; the
send-to-PayPal / accept-claim buttons; and an unused `ProcessedWebhookEvent` dedup model.

Missing or broken (code-verified):
- **No inbound dispute path** — `PayPalWebhookView` (`apps/payments/views.py`) handles only refund events.
- **Outgoing API calls would fail** — `apps/payments/paypal_disputes_service.py` uses the wrong path
  `/v1/customer-disputes/{id}` (hyphen; PayPal uses `/v1/customer/disputes/{id}`, slash) at lines
  160/234/367/460, and `provide_evidence` sends base64-in-JSON `supporting_files` (lines 238-264) instead
  of file uploads, with **no `evidence_type`** set.
- **Live-only** — base URL hardcoded to `https://api.paypal.com` (lines 75/159/233/366/459); no sandbox.
- **No lifecycle-stage field** on `Dispute`; **reason enum uses American `UNAUTHORIZED_TRANSACTION`** where
  PayPal uses `UNAUTHORISED` (spelling/format must match PayPal exactly).
- **No deadline tracking/alerts**, **no won/lost status sync**, **no category dropdown / per-category reports**.

## The PayPal Disputes API in brief (⚠ confirm in sandbox before building — research, medium confidence on file limits)

- **Receive:** thin webhook `CUSTOMER.DISPUTE.CREATED` (+ `.UPDATED`, `.RESOLVED`) carrying the dispute id;
  verify it is genuinely from PayPal (signature verification using the configured `webhook_id`), then fetch.
- **Read:** `GET /v1/customer/disputes/{id}` → `dispute_id`, `status`, `reason`, `dispute_amount`,
  `buyer`, `disputed_transactions`, `messages`, `dispute_life_cycle_stage`, `seller_response_due_date`.
- **Submit:** `POST /v1/customer/disputes/{id}/provide-evidence` (multipart file upload + per-file
  `evidence_type` label); or `/accept-claim` to concede+refund; `/send-message` at the inquiry stage.
- **Stage gates the action:** `INQUIRY` → can only message; evidence upload is rejected until
  `CHARGEBACK`/`PRE_ARBITRATION`/`ARBITRATION`. Stage can move backward — re-read on every refresh.
- **Deadline:** `seller_response_due_date`; **missing it = automatic loss + buyer refund.** Highest-stakes
  fact in the pipeline.

## Decisions (with the user)

| Decision | Choice |
|---|---|
| Evidence rendering | Render fetched Zendesk data as faithful, **clearly-labeled** Zendesk-styled panels → PDF. NOT browser screenshots (Playwright deprecated for this pipeline). Panels are verbatim reproductions of real data, never embellished. |
| Categorization | A human reads each dispute and **picks its category from a dropdown**; the category drives which report template is used. Prefill the dropdown from PayPal's `reason`; human can override. |
| Reports | **Per-category** — each category has its own report layout (exact layouts pending the user's report model). |
| Trust boundary | AI narrative paragraphs go through `AIClient` (PII tokenized; LLM provider is outside the zone). The finished report to PayPal may contain real client data (PayPal is inside the zone). |
| Scope sequencing | Build report-independent phases first (1-3), then categorization + gating (4), then per-category reports (5), then polish (6). |

---

## Phase 1 — Fix the outgoing pipe (report-independent)
**Goal:** the calls LORA makes to PayPal actually succeed, and can be tested without real money.
**Design:** in `paypal_disputes_service.py` — correct all paths to `/v1/customer/disputes/{id}`; rewrite
`provide_evidence` to a multipart file upload with a per-file `evidence_type`; fix `/message` →
`/send-message`; add a sandbox/live switch (a `SystemSettings.paypal_mode` reading
`api-m.sandbox.paypal.com` vs `api-m.paypal.com`) shared with the refund/connection-test code so the dead
`PAYPAL_MODE` story is finally real.
**Files:** `apps/payments/paypal_disputes_service.py`, `apps/config/models.py` (+ settings/admin field).
**Acceptance:** sandbox calls for fetch / provide-evidence / accept-claim / send-message succeed against a
sandbox dispute; live path unchanged in shape; unit tests mock the HTTP layer and assert path + multipart
shape + evidence_type.
**Depends on:** PayPal credentials with Disputes permission; a sandbox account.

## Phase 2 — Open the inbound door (report-independent)
**Goal:** disputes arrive in LORA automatically.
**Design:** extend the PayPal webhook to handle `CUSTOMER.DISPUTE.CREATED` — **verify the PayPal signature**
(real verification using `webhook_id`, not the shared-secret stopgap used elsewhere), then fetch full
details, create/De-dupe the `Dispute` (via `ProcessedWebhookEvent`), match it to a Claim (by
`transaction_id`/order/buyer email), and store `seller_response_due` + `raw_webhook_payload`. Map PayPal's
`reason`/stage onto the model (see Data-model changes).
**Files:** `apps/payments/views.py` (or a dedicated dispute webhook view), `paypal_disputes_service.py`,
`apps/payments/models.py`.
**Acceptance:** a sandbox `CUSTOMER.DISPUTE.CREATED` creates exactly one matched Dispute with the deadline
captured; a forged/duplicate event is rejected; unmatched disputes are stored unlinked, not dropped.
**Depends on:** Phase 1 (fetch must work); a PayPal webhook subscription + `webhook_id` configured.

## Phase 3 — Deadline safety + status sync (report-independent)
**Goal:** never miss a deadline; won/lost flows back automatically.
**Design:** surface `seller_response_due` prominently (dispute list + detail countdown, colour-coded; the
manager dashboard gets a "disputes due soon" counter mirroring the claims work). Handle `CUSTOMER.DISPUTE.UPDATED`/
`.RESOLVED` → update `Dispute.status` (RESOLVED_WON/LOST) and reflect on the linked Claim. Deadline
alerting per the user's choice (banner / email / both).
**Files:** webhook view, `apps/users/views.py` (dashboard/dispute list), templates.
**Acceptance:** an updated/resolved sandbox dispute updates the record + claim; a dispute within N days of
its deadline shows the alert; resolved disputes leave the "due soon" counter.
**Depends on:** Phase 2.

## Phase 4 — Category dropdown + stage gating
**Goal:** the human picks the category that drives the report; the UI never lets an invalid action fire.
**Design:** add `dispute_life_cycle_stage` to the model; **fix the reason enum to PayPal's exact values
(incl. British `UNAUTHORISED`)** via a data migration. On the detail page, a category dropdown prefilled
from the fetched `reason`, editable by the human, persisted on the Dispute. Disable "Submit evidence" unless
the stage permits it (inquiry = message-only); re-read stage on refresh.
**Files:** `apps/payments/models.py` (+ migration), dispute detail view/template.
**Acceptance:** category persists and selects the report path; Submit-evidence is disabled at INQUIRY and
enabled at CHARGEBACK+; reason values round-trip with PayPal unchanged.
**Open question:** is "category" exactly PayPal's `reason`, or a separate human taxonomy that *maps from*
reason? (Affects whether we add a `category` field distinct from `dispute_reason`.)

## Phase 5 — Per-category evidence reports (the report-model work)
**Goal:** one report per category, each a Zendesk-styled PDF built from fetched data.
**Design (framework now, layouts later):** a reusable **evidence-bundle assembler** (one structured object
with everything a report could need: ticket fields, full comment thread, emails, payment/refund history,
flight verification, timeline) + a **panel-rendering component** (Zendesk-styled, labeled "exported from
ticket #X on <date>", verbatim data) + a **category→template registry** so each category renders its own
layout. Reuse the existing WeasyPrint PDF path. AI narrative paragraphs via `AIClient`.
**Files:** `apps/payments/document_service.py`, new templates per category.
**Acceptance:** for each supported category, a faithful PDF is produced from real ticket data and can be
sent via Phase 1's `provide-evidence`.
**Depends on / BLOCKED until provided:** the user's **report model** for each category (layout, sections,
paragraph copy, which panels). The *framework* (bundle + panel component + registry) is buildable now; the
*per-category layouts* are not.

## Phase 6 — Polish
State-machine guardrails (no jumping straight to ACCEPTED), sanitize the AI's HTML before display,
idempotent document generation, assignment (`assigned_to`) + notes UI, manager notifications.

---

## Open questions to confirm before building
1. **PayPal credentials** — do the existing `paypal_client_id`/`secret` have **Disputes API** permission?
2. **Webhook subscription** — is a PayPal **webhook subscription + `webhook_id`** configured? (Required to
   verify inbound disputes; Phase 2 blocks without it.)
3. **Deadline alerts** — banner, email, or both?
4. **Category model** — PayPal `reason` directly, or a separate human taxonomy mapped from it?
5. **The report model** — per-category layouts (Phase 5).

## Risks
- **Deadline is unforgiving** — design alerting so a deadline cannot pass silently.
- **Evidence upload format unverified** — multipart shape + file-size limits (10 MB/50 MB per research;
  older docs cite 4 MB/5 MB) must be proven in **sandbox** (needs Phase 1's switch) before trust.
- **Reason/stage string drift** — any mismatch with PayPal's exact enums breaks matching and submission.
- **Money-touching + outward-facing** — accept-claim refunds a client; submit-evidence is irreversible per
  cycle. These need confirmation steps + state guards (Phase 6) and a supervised first live run.
