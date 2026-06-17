# LORA — Main Application Flows (plain English)

Plain-English walkthrough of the ten core flows, traced against the live code.
Each section names the load-bearing functions (`path:line`) and calls out the
non-obvious design decisions. Written 2026-06-17; line numbers are accurate as
of that date and will drift — treat them as starting points, not guarantees.

## Five facts that run through everything

- **Zendesk is the system of record.** A LORA `Claim` is a *mirror* of a Zendesk
  ticket so LORA can run AI, scheduling, and cross-system logic on top.
- **Clients never email in.** They use an external web form that becomes a
  Zendesk ticket. Every email LORA *fetches* is from an institution (a
  lost-&-found office).
- **One claim is sent to many offices.** So "not found" from one office is never
  the claim's outcome — aggregation across offices happens at the claim level,
  never in the per-email categorizer.
- **PII is masked only for the LLM provider.** Zendesk and PayPal are *inside*
  the trust boundary and get real values; the language model gets tokenized
  placeholders.
- **One trusted staff user; PayPal defaults to sandbox.** No role checks; no
  PayPal action moves real money until `paypal_mode` is explicitly `live`.

---

## 1. How a claim is created

Every claim originates from a Zendesk ticket, and all routes funnel through one
function: `create_claim_from_zendesk_ticket` (`apps/integrations/services.py:439`).
It fetches the ticket, then applies the real filter — **the ALF-claim-number
gate**: the ticket must carry an `ALFxxxxxxx` code (in the subject or a "Claim #"
field), otherwise it's ignored as "not a claim." If it passes,
`analyze_zendesk_ticket_for_claim` (`apps/integrations/services.py:1064`) reads
the structured Zendesk custom fields directly for nearly everything (email, name,
phone, flight, deadline, payment, addresses) and only asks the LLM for the
free-text *object description*. Structured fields always win; the LLM just fills
blanks.

**Four entry points, but only one *originates* claims:**

- The **status-change webhook** (`apps/integrations/views/webhooks.py:220`) — the
  normal automatic path: a ticket entering "Investigation Initiated" creates the
  claim.
- **Inbound institution email**, the **manager bulk-import screen**, and the
  **REST API** all only *copy* an already-existing ticket (via the
  `import_claim_from_zendesk_ticket` wrapper, `apps/integrations/services.py:605`)
  — they never fabricate one.

**By design / watch out:**

- There is **no intake form inside this app** and no `claims/forms.py`. Email
  bodies never become claim content — an email only *triggers* copying the ticket.
- `client_email` is the only strictly-required DB field. If no email can be
  resolved, the claim is still created but flagged `llm_extraction_failed=True`
  for manual review.
- Duplicate-proof three ways: an upfront existence check, a unique constraint on
  `zd_ticket_id`, and `IntegrityError` recovery on a race.
- The AI summary runs *after* the claim is committed and never blocks or rolls
  back creation.

---

## 2. How emails are fetched

LORA pulls institution replies from **one shared IMAP mailbox**. The live path is
**button-driven and per-ticket**: a staff member clicks "check email" on a claim
page (`apps/claims/views.py:273`) or in the Zendesk sidebar
(`apps/integrations/views/email.py:26`), and LORA searches the mailbox for new
mail addressed to *that one ticket's* alias. `open_inbox`
(`apps/communications/services.py:946`) reads the mailbox credentials from
`SystemSettings` (the password is Fernet-encrypted at rest), opens a TLS IMAP
connection, and `search_alias_uids` (`apps/communications/services.py:963`)
searches **UNSEEN mail from the last 2 days** across three headers (To,
Delivered-To, X-AnonAddy-Original-To) and unions the results.

**By design / watch out:**

- A **global inbox sweep** (`process_incoming_emails`,
  `apps/communications/services.py:789`) exists and is wired into the cron
  dispatcher, but it's **dormant** — gated off by `email_sweep_autorun`
  (default False). Normally email is fetched only when a human clicks.
- **Two-layer dedup on the RFC-5322 Message-ID**: a cheap existence query plus a
  DB unique constraint that wins same-second races (the loser catches
  `IntegrityError` and counts it "already processed"). This is *not* keyed on the
  read/unread flag.
- The `EmailLog` insert is deliberately **not** wrapped in a transaction with the
  external work (LLM, Zendesk note, IMAP flag) — rolling back a genuinely-
  processed email would cause it to be re-processed and re-posted. Message-ID
  dedup is what makes that safe.
- 2-day window because the unread flag can't guarantee once-only (mail needing a
  human is *left* unread on purpose). Per-run cap of 20 AI-processed emails;
  dedup skips don't count, so a re-click resumes.
- The alias is validated with an anchored regex before being interpolated into
  the raw IMAP search string (injection guard).

---

## 3. How emails are linked to claims

**The alias is the only linking key.** When a claim goes out to many offices,
every office replies to that one per-ticket alias address, so all replies funnel
back through it. `match_alias_to_zendesk_ticket`
(`apps/integrations/services.py:914`) reads which alias the message was addressed
to, asks Zendesk "which ticket has this alias in its custom field?", and then
finds the Claim whose `zd_ticket_id` matches. Its own comment says it plainly:
*"This is the ONLY matching method - no fallback to other fields."*

The linkage is stored on an `EmailLog` row (`apps/communications/models.py:4`) —
a nullable FK to the claim (`related_name='emails'`), plus the ticket-id string,
the matched alias, the true sender, the AI category/summary, and the Message-ID.

**By design / watch out:**

- Sender address, subject, and **claim number are NOT used to link replies** —
  the ALF number is only used when *creating* a claim, not when attaching replies.
- **No match is non-destructive**: the email is still logged. In the (dormant)
  sweep, an alias matching no ticket is saved with `claim=null` and
  **deliberately not posted to Zendesk** — only alias-matched mail is posted, to
  avoid wrong-ticket attribution. Worst case is an orphaned logged email, never a
  misattributed one.
- AnonAddy rewrites the From header; the true institution sender is recovered
  from `X-AnonAddy-Original-Sender` or by decoding the encoded address.

---

## 4. How emails are read and interpreted

Each email's meaning is decided in one place: `call_qwen_ai`
(`apps/communications/services.py:315`), which goes through `AIClient.complete`
(`apps/ai/client.py:105`). Because the LLM provider is outside the trust
boundary, the client's known identifiers are tokenized first — the
`RegexTokenizer` (`apps/ai/tokenizer.py:87`) replaces names, emails, phones, ALF
IDs and flight-codes with deterministic HMAC placeholders (e.g.
`<NAME_a3f9b2c1>`), keeps an in-memory map, and restores real values on the way
back. The email is wrapped in XML fences with a defense preamble telling the
model to treat fenced text as data, not instructions (prompt-injection defense).
The reply is forced into the `EmailCategorization` schema (`apps/ai/schemas.py:24`):
a summary, one `category` (OBJECT_FOUND / OBJECT_NOT_FOUND / RESUBMISSION_REQUIRED
/ SUBMISSION_CONFIRMATION / GENERAL_CORRESPONDENCE / UNKNOWN), and two booleans.
If validation fails, it falls back to a legacy keyword parser.

**The per-office logic is NOT in the categorizer** — each email is classified in
isolation. Aggregation happens later at the claim level in
`apps/communications/client_updates.py`:

- `object_found(claim)` (`apps/communications/client_updates.py:334`) treats a
  "found" from **any** office as the good-news signal (always held for a human,
  never auto-sent).
- Before drafting a client update, a **deterministic allowlist**
  (`CLIENT_SAFE_REPLY_CATEGORIES`, `apps/communications/client_updates.py:71`)
  drops every OBJECT_NOT_FOUND / RESUBMISSION / UNKNOWN reply *before the model
  sees it*. The "many offices, one claim, a 'not found' is just one office"
  framing in the prompt is explicitly belt-and-suspenders — the allowlist is the
  real guard.

**Watch out:** PII masking of free-text *names* requires a linked claim (to know
what to mask). A claimless institution email gets only the generic regex tokens —
a quoted client name could reach the provider. This is acknowledged in the code.

---

## 5. How the Zendesk update for a claim works

This syncs in **both directions**.

**Zendesk → LORA (status):** When a ticket's custom status changes, Zendesk fires
the webhook to `ZendeskClaimWebhookView` (`apps/integrations/views/webhooks.py:220`)
(rejected with 401 if the shared secret header is wrong). `mirror_status_change`
(`apps/integrations/views/webhooks.py:137`) translates the numeric status id into
a name + family via `resolve_custom_status` (`apps/integrations/services.py:418`,
cached 24h), then — in **one transaction** — updates `status`/`status_category`/
`status_changed_at` and writes a `ClaimUpdateTimeline` history row. **This webhook
is the only writer of a claim's workflow stage.**

**Zendesk → LORA (full field refresh):** A staff button hits
`ClaimUpdateFromZendeskView` (`apps/claims/views.py:221`) →
`refresh_claim_from_zendesk` (`apps/claims/services.py:128`), which re-reads the
ticket and merges fields (overwrite vs fill-only) — but **never touches status**
(the webhook owns that).

**LORA → Zendesk (write-backs):** creating a ticket for a claim that has none; on
a fresh refund, *additively* tagging "refunded" (`add_zendesk_ticket_tags`,
`apps/integrations/services.py:885`) and posting an internal note.

**By design / watch out:**

- Two idempotency guards in the mirror: a same-status payload is a no-op (handles
  Zendesk retries), and an *unresolved* status id returns **HTTP 503 rather than
  overwriting a real status name with a raw number**.
- The status update + history row are atomic; the AI summary back-fill runs after
  and is intentionally *not* swallowed, so an AI failure 500s and lets Zendesk
  retry the whole webhook.
- `ai_summary` is written in exactly one function, `refresh_claim_summary`
  (`apps/integrations/briefing.py:123`); the read-only sidebar briefing shares
  the prompt but never writes it. Tag write-back is **additive** (an older
  version replaced all tags — a comment warns never to revert).

---

## 6. How refunds work

Every refund is a row in the `Refund` table (`apps/payments/models.py:8`) — the
money audit trail. Four ways a row is born, all through `RefundService`
(`apps/payments/refund_service.py`):

- **Issue via PayPal-direct** (`initiate_refund`,
  `apps/payments/refund_service.py:59`) — calls PayPal's refund API. *Largely
  vestigial*: it hard-fails without a `capture_id`, and nothing in the live flow
  currently supplies one.
- **Issue via the WooCommerce "reverse lever"** (`issue_woocommerce_refund`,
  `apps/payments/refund_service.py:440`) — the **real** LORA-initiated path: LORA
  asks WooCommerce to refund the order through the gateway, and WooCommerce is the
  *sole* thing that moves money. Double-paying becomes structurally impossible.
- **Manual record** (`create_manual_refund`, `apps/payments/refund_service.py:350`)
  — staff record an already-issued refund; written COMPLETED, deduped within a
  60-second window.
- **Inbound webhook recorder** (`process_woocommerce_refund`,
  `apps/payments/refund_service.py:529`) — the main real-world path: WordPress
  notifies LORA, which records COMPLETED.

The shared safety net is `_reserve_refund` (`apps/payments/refund_service.py:392`):
under a **row lock** it sums all PENDING+PROCESSING+COMPLETED refunds for the
claim and refuses anything that would exceed `price_paid` (the over-refund cap),
creates a PENDING row *first*, then calls the gateway **outside the transaction**
so a timeout can't roll back and "forget" a refund that actually went through.

**By design / watch out:**

- Reconciliation collapses to **one row**: if LORA pulled the reverse lever
  (leaving a `WC-PENDING-` reservation) and the inbound webhook then arrives, it
  **adopts and finalizes that same row** rather than creating a second.
- An indeterminate WooCommerce result (timeout) stays PENDING (still counts
  against the cap) and returns 502 — "go check WooCommerce," don't blind-retry.
- A separate secured `PayPalWebhookView` (`apps/payments/views.py:34`) can record
  a PayPal `CAPTURE.REFUNDED` event, but it's explicitly a **fallback**
  (X-Webhook-Secret header, not a real PayPal signature) using the
  `ProcessedWebhookEvent` idempotency table.
- The refund viewset is **GET/POST only** — no PUT/PATCH/DELETE — so a completed
  money record can't be rewritten or erased. Refunds are USD-only.

---

## 7. How disputes work (overall)

LORA treats a PayPal dispute (a buyer challenging a charge) as its own case
object with a small lifecycle, all in `apps/payments/paypal_disputes_service.py`
and `apps/payments/document_service.py`: **receive → human categorizes → build
evidence → submit or accept.**

A single staff member works each dispute from its detail page: pick the PayPal
reason category, generate a polished **evidence PDF**, prepare the **written
argument** PayPal's reviewer reads, then either **submit** (fight) or **accept**
(concede with a refund). The submit endpoint is auto-picked from the dispute's
PayPal state (see flow 9). Conceding via `accept_claim`
(`apps/payments/paypal_disputes_service.py:364`) issues a refund guarded by three
independent double-refund protections: a terminal-status pre-check, a DB mutex on
the dispute row, and a stable `PayPal-Request-Id` idempotency key. Everything
defaults to **sandbox** until `paypal_mode` is flipped to `live`.

**Watch out:** the "evidence report" PDF and the "narrative notes" are **two
separate things with two separate AI calls** — the PDF is template-based; the
written argument PayPal reads is plain text. The old AI "response letter" PDF was
removed. A couple of enum statuses (`GATHERING_DATA`, `DOCUMENTS_READY`) appear
defined but unused — the live workflow leaves a dispute at MATCHED until it's
submitted/accepted/resolved.

---

## 8. How disputes arrive on a claim

Two doors, both funneling into `ingest_dispute`
(`apps/payments/paypal_disputes_service.py:849`). The primary is the **webhook**
to `PayPalDisputeWebhookView` (`apps/payments/views.py:102`): since PayPal posts
with no shared secret, authenticity is proven by calling PayPal *back* —
`verify_webhook_signature` (`apps/payments/paypal_disputes_service.py:723`),
**fail-closed** (anything other than "SUCCESS" → 401). Then it claims the event
in `ProcessedWebhookEvent` (idempotency), and on failure **deletes that claim and
returns 503** so PayPal retries. The second door is the manual **"Pull from
PayPal"** button (`dispute_pull_from_paypal`, `apps/payments/frontend_views.py:297`)
for disputes that predate the webhook subscription.

**The match — the heart of "landing on a claim"** — is `_match_claim_for_dispute`
(`apps/payments/paypal_disputes_service.py:774`). PayPal **does not include the
buyer's email**, so matching tries, in order: (1) the ALF claim id embedded in the
**invoice number**, (2) the **PayPal transaction id**, (3) the **WooCommerce
order id**, (4) email only as a theoretical last resort.

**By design / watch out:**

- This webhook uses PayPal's **signature**, unlike the refund webhook which uses
  an `X-Webhook-Secret` header — two different auth schemes for two different
  PayPal endpoints.
- **Double-verification:** if the ALF-matched claim and the dispute both carry a
  transaction id and they *disagree*, it refuses to auto-link by ALF and falls
  through.
- **Unmatched is a first-class state:** a dispute with no claim is still created
  (`claim=None`, status RECEIVED) for a human to link later; matched ones are
  MATCHED with the claim's Zendesk ticket id copied over.
- `ingest_dispute` **self-heals**: an existing unmatched row gets re-matched on
  re-pull (older rows were matched by the useless buyer email).

---

## 9. How dispute back-and-forth messaging works

A dispute is a two-sided conversation, but **there is no message table.** LORA
keeps two halves and merges them only at display time:

- **Our side** = `DisputeSubmission` rows (`apps/payments/models.py:566`), one per
  reply staff send.
- **Their side** (buyer + PayPal messages, plus PayPal's evidence list) is never
  stored individually — it lives inside the single `raw_webhook_payload` JSON blob
  that `sync_dispute_from_paypal` (`apps/payments/paypal_disputes_service.py:943`)
  **overwrites wholesale** every time LORA fetches the dispute.

Sending: staff type into one composer (or "Draft with AI"), saving a DRAFT
submission (`dispute_prepare_submission`, `apps/payments/frontend_views.py:705`);
"Submit to PayPal" (`dispute_submit_to_paypal`, `apps/payments/frontend_views.py:758`)
atomically flips DRAFT→SUBMITTING and calls `submit_dispute_response`
(`apps/payments/paypal_disputes_service.py:671`). **The channel is auto-picked,
not chosen** by `Dispute.submit_endpoint` (`apps/payments/models.py:386`):
`provide-evidence` for the first response, `provide-supporting-info` once under
review, or nothing (button disabled) at inquiry/resolved. Receiving is **entirely
pull-based** — new buyer/PayPal messages only appear after an inbound webhook or
the manual "Refresh from PayPal." `build_dispute_reply_timeline`
(`apps/payments/document_service.py:1516`) merges submissions + the payload's
`evidences[]` + `messages[]` into one chronological "case log," using `posted_by`
to label each speaker (BUYER → Buyer, ARBITER/PAYPAL → PayPal).

**By design / watch out:**

- There's a `send_message` function (`apps/payments/paypal_disputes_service.py:454`)
  and a `KIND_MESSAGE` enum for the inquiry-stage "message the buyer" channel —
  but it's **dead code**: no route calls it, so the inquiry stage is effectively
  reply-disabled ("No reply window open").
- Outbound submissions deliberately send **no idempotency key and are not
  auto-retried** — a partly-landed evidence upload must not be blindly re-POSTed.
  The only guard is the local DRAFT→SUBMITTING flip against double-clicks.
  (Contrast: accept-claim, which moves money, *does* use a request-id + mutex.)
- "Buyer" here is the paying airport client — distinct from the lost-&-found
  offices in the email flows.

---

## 10. How dispute evidence generation works

The PDF entry point is `generate_evidence_report`
(`apps/payments/document_service.py:1579`). The data-gathering heart is
`build_dispute_evidence_bundle` (`apps/payments/document_service.py:1199`), which
pulls: the full Zendesk ticket + all comments (rendered as **"simulated
screenshot" panels** — this is where the offices' replies live, pasted by staff),
the claim's evidence photos (base64-encoded after a path-traversal guard and
**MIME-sniffing the real bytes**, not the filename), the email history, a flight
card rebuilt from stored flight data, an **IP cross-check** (does the IP the
client emailed from match the one they submitted the form from?), fixed
homepage/checkout screenshots, and per-reason framing text. An AI pass
(`_narrate_evidence`, `apps/payments/document_service.py:1120`) sorts each piece
into narrative sections with a one-line relevance note (falling back to one
ungrouped section if AI is down). `_render_to_pdf`
(`apps/payments/document_service.py:196`) feeds the HTML + a Zendesk-styled
stylesheet to **WeasyPrint**, and `_persist_document`
(`apps/payments/document_service.py:486`) saves it as an auto-versioned
`DisputeDocument` (DRAFT) under a row lock.

The **written argument** PayPal reads is separate: `build_dispute_narrative_notes`
(`apps/payments/document_service.py:1454`) writes four prose sections (opening /
authorization / service-delivery / closing), with PII tokenized before the LLM
and restored after (PayPal gets the real values). Submission attaches the latest
evidence PDF + staff images via `_build_submission_files`
(`apps/payments/paypal_disputes_service.py:618`) and `_encode_multipart`
(`apps/payments/paypal_disputes_service.py:78`).

**By design / watch out:**

- Two outputs, two AI calls: the narrative-notes path calls the bundle with
  `use_ai=False, embed_attachments=False`, so it does *not* run the per-record
  sorter or download images.
- **Upload validation** (`validate_evidence_image`, `apps/claims/services.py:13`)
  requires a real image (Pillow `verify()`, not just the extension), ≤10MB. Note
  this guards *uploads* only — *embedded* Zendesk/inline images are MIME-sniffed,
  skipped under 64px (tracking pixels), and downscaled.
- Zendesk image security: attachment URLs are fetched with a browser UA and **no
  auth token sent to foreign hosts** — only `*.zendesk.com` / signed
  `*.zdusercontent.com` are allowed.
- Manually-created disputes (`MANUAL-` ids) return an empty submit endpoint —
  staff download the PDF and upload it in PayPal by hand. PDF/notes are
  size-*warned*, not hard-capped.

---

## Things not verifiable from code (deployment config, not bugs)

- Which PayPal events are actually subscribed in the live PayPal account.
- The exact PayPal multipart wire format and accepted `evidence_type` values (the
  code itself says "confirm against sandbox before live").
- Live values of operational toggles such as `email_sweep_autorun` and
  `import_claims_from_email`.
