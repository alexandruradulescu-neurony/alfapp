# Claim Entity Redesign — Status Mirror + Real Summary Engine

**Date:** 2026-06-11
**Status:** Implemented 2026-06-11 (see docs/superpowers/plans/2026-06-11-claim-status-mirror.md)
**Scope anchor:** Zendesk is the source of truth and where agents work. LORA is
management's lens and the case's hub: it links refunds/evidence/emails/problems
to one claim, keeps a truthful index of where every case stands, and produces
AI summaries at lifecycle points. LORA does not duplicate Zendesk; it extracts,
summarizes, and surfaces. (Management "one screen" views are the NEXT round —
this round builds the foundations they need.)

## Problems being fixed

1. **Status list can't tell the whole story** — no "not found", "delivered", or
   "closed" endings; cases sit in "Searching"/"Shipped" forever.
2. **One field, two jobs** — item journey and money journey share `status`;
   refund completion overwrites the stage (e.g. `REFUNDED` erases `Shipped`).
3. **Mixed naming styles** — `'Received'` vs `'REFUND_REQUESTED'`.
4. **Deadline not computable** — date is real; time + timezone are free text.
5. **Dormant `__str__` bug** — `ClaimUpdateTimeline.__str__`
   (apps/claims/models.py:260) contains Django template syntax inside an
   f-string; crashes on first `str()`.
6. **(Found during design) The claim's "AI summary" is not AI** — both writers
   (apps/integrations/views.py:1142 creation; apps/claims/views.py:376 the
   "Update from Zendesk" button) glue extracted fields into one sentence
   ("Client: X. Flight: Y. …"). Timeline `llm_summary` entries are equally
   mechanical. The manager chat consumes this glue-text
   (apps/agent/services.py:324) — management's chat is built on the weakest
   summary in the system while the real briefing engine serves the sidebar.
7. **(Found during design) Dashboards under-count active work** — "active" =
   `['Received', 'Searching']` only (apps/users/views.py:133, 541-542);
   Found/Shipped claims silently vanish from active lists.
8. **(Found during design) Webhook auth is optional** — both webhook views
   verify `X-Webhook-Secret` ONLY when the header is present; absent header =
   no check (apps/integrations/views.py:878-887, 1010-1020). Also the claim
   webhook logs ALL request headers at INFO (views.py:986), leaking the secret
   into Railway logs.

## Decisions (made with the user)

| Decision | Choice |
|---|---|
| Where stage truth lives | **Zendesk drives it.** LORA mirrors; LORA never writes stages. |
| Status model | **Mirror Zendesk exactly** (agent-view status names verbatim) + Zendesk's status *family* (category) for grouping + `status_changed_at`. |
| Existing prod data | Test claims only → simple remap migration, no ceremony. |
| Summary regeneration | **Creation + every status change + manual button.** |
| Round scope | Foundations now (mirror, summaries, count fixes); management one-screen view next round. |

## The Zendesk status vocabulary (from the user, 2026-06-11)

Agent-view name → meaning (client-facing name where it differs):

- **New** — awaiting assignment (client sees "Open")
- **Open** — ready to be worked (client sees "Open")
- **Investigation initiated** — staff working the ticket (client sees "Open").
  *Claim-creation trigger; custom status id 11688538967068.*
- **Claim submitted** — submitted to third parties/institutions (client sees
  "Search in progress")
- **Object Found** — item located (client sees "Object Found")
- **Refund Requested** — client asked for refund; management approval pending
- **Pending** — waiting for the requester to reply (client sees "Awaiting your reply")
- **Solved** — ticket solved
- **Solved - Object Found** — case ended successfully, object recovered
- **Closed - Object Not Found** — search failed, case closed
- **Closed - Client Not Answering** — closed for unresponsive client
- **Closed - Refunded** — closed with refund
- **Refund-Denied** — refund denied after client confirmation; search stops,
  ticket closes

This table goes into `ALF_BUSINESS_CONTEXT` so all AI output (briefings, chat,
drafts, stored summaries) understands the exact workflow position and what the
client currently sees.

## Design

### 1. Claim model changes (apps/claims/models.py)

- `status` — now stores the Zendesk agent-view status name verbatim. Drop
  `STATUS_CHOICES`/`choices=`; validation = non-empty; unknown names are
  accepted and logged (so a renamed Zendesk status never bounces). Default for
  new claims: `'Investigation initiated'`.
- NEW `status_category` — Zendesk's family for the status: one of
  `new | open | pending | hold | solved` (empty string when unknown). Indexed;
  dashboards group/color by it.
- NEW `status_changed_at` (DateTimeField, null) — when the current status was
  set; enables "stuck case" detection later.
- NEW `deadline_at` (DateTimeField, null) — computed deadline moment (see §6).
- NEW `ai_summary_updated_at` (DateTimeField, null) — freshness of the stored
  summary.
- `ai_summary` — unchanged field, but now written ONLY by the summary engine.
- Fix `ClaimUpdateTimeline.__str__` (problem 5).

**Migrations:** one schema migration; one data migration remapping old values:

| Old | New status | Category |
|---|---|---|
| Received | Investigation initiated | open |
| Searching | Claim submitted | open |
| Found, Shipped | Object Found | open |
| Disputed | Open | open |
| REFUND_REQUESTED | Refund Requested | open |
| REFUNDED, PARTIALLY_REFUNDED | Closed - Refunded | solved |

Backfill `status_changed_at = updated_at`, `deadline_at` via the parser (§6).
Categories above are best-guess defaults; at runtime the resolver (§2) is the
authority. (Prod data is test-only; mapping exactness is not critical.)

### 2. Custom-status resolver (apps/integrations/services.py)

`resolve_custom_status(status_id: str) -> {'name': str, 'category': str}`

- Source: `GET /api/v2/custom_statuses.json` with existing SystemSettings
  Zendesk credentials (same `_get_zendesk_auth_headers()` plumbing).
- Cache the id→(name, category) map (Django cache, 24h TTL). Unknown id →
  force one refresh → still unknown → return `{'name': '<id>', 'category': ''}`
  and log a warning. New/renamed statuses in Zendesk therefore flow through
  with no code change.
- **Dependency:** SystemSettings `zd_subdomain`/`zd_email`/`zd_token` must be
  configured in prod (already required for claim creation; user is filling
  them in).

### 3. Webhook becomes the single stage writer (ZendeskClaimWebhookView)

`POST /api/integrations/zd/claim-webhook/` already receives EVERY
`ticket.custom_status_changed` event. New behavior:

1. **Auth required:** missing or wrong `X-Webhook-Secret` → 401 (today:
   absent header skips the check). Remove the header-dump DEBUG logging
   (problem 8). Secret remains `SystemSettings.sidebar_secret_token`.
2. Resolve `event.current` via §2.
3. **Claim exists for ticket:** if resolved name == current `claim.status` →
   no-op 200 (idempotent under Zendesk retries). Else update `status`,
   `status_category`, `status_changed_at=now`; write a `ClaimUpdateTimeline`
   entry (`STATUS_CHANGE`, `changes_summary` JSON with old→new,
   `llm_summary` = fresh summary text); regenerate the stored summary (§5).
4. **No claim + status == Investigation initiated** (id 11688538967068, kept
   as a constant): create the claim (existing extraction flow), with
   `status='Investigation initiated'` + category from resolver; the stored
   summary comes from the summary engine (§5) instead of the glue text.
   Existing race handling (IntegrityError → return winner) stays.
5. **No claim + any other status:** ignore with 200 (current behavior).

**AI-failure tolerance:** stage updates and timeline entries must succeed even
when the AI call fails — catch, log, keep the previous `ai_summary`, write the
timeline entry with `llm_summary=''`. A stage change is never lost to an AI
hiccup. (AI runs inline in the webhook — same as today's creation extraction;
idempotency + Zendesk retries cover timeouts.)

### 4. LORA stops writing stages — every writer removed

- `ClaimViewSet.update_status` action (apps/claims/views.py:97) — removed.
- `agent_update_status` view (apps/users/views.py:304) + its URL + the
  dropdown form in templates/agent/claim_detail.html — removed; replaced by a
  read-only stage card + "Open ticket in Zendesk" link
  (`https://{zd_subdomain}.zendesk.com/agent/tickets/{zd_ticket_id}`,
  subdomain from SystemSettings).
- `refund_service.py` lines 116/118/366/368 — stop assigning `claim.status`.
  Money truth = Refund/Dispute records (already shown as record-based badges).
- `ZendeskStatusWebhookView` (+ `zd/status-webhook/` URL) — **deleted**. Its
  flat `{ticket_id, status, claim_id}` payload is superseded by the generic
  mirror; its Refund `get_or_create` is broken anyway (omits the unique
  `paypal_refund_id`). Rollout step: confirm no Zendesk webhook targets this
  URL before removal (likely never wired — payload shape matches no Zendesk
  event).
- Claims with no `zd_ticket_id` (legacy/test only): stage is read-only too.

### 5. One summary engine (new module: apps/integrations/briefing.py)

`generate_claim_summary(claim, ticket_data) -> str | None`

- Context: `build_claim_facts(claim)` + `build_ticket_thread(ticket_data)`
  (server-fetched comments normalized to the dict shape build_ticket_thread
  expects) + `ALF_BUSINESS_CONTEXT` (now including the status vocabulary).
- Runs through `apps/ai/AIClient` with the BriefingSummary-style schema,
  `known_pii={'names': [claim.client_name, requester_name]}` — same PII rules
  as the sidebar. The stored (untokenized) summary lives in LORA's DB, which
  is inside the trust zone.
- Writers of `claim.ai_summary` + `ai_summary_updated_at`: webhook creation,
  webhook status change, manual refresh (§5a). Nothing else.
- The sidebar briefing endpoint refactors to SHARE the prompt/context
  internals but keeps its own data source (app-supplied comments) and does
  NOT write the stored summary (avoids surprise writes from agent clicks).
- Manager chat needs no change — it reads `claim.ai_summary`, which becomes
  real.

### 5a. "Refresh from Zendesk" button (replaces "Update from Zendesk")

`ClaimUpdateFromZendeskView` (apps/claims/views.py:236) is rebuilt:

- Fetch ticket + comments (existing services).
- Run the full 17-field extractor (`analyze_zendesk_ticket_for_claim`).
  **Update policy:** values originating from structured Zendesk fields
  overwrite the claim (Zendesk = truth); LLM-inferred values (from comment
  text) fill blanks only.
- Recompute `deadline_at`; regenerate the summary; write an `INFO_UPDATED`
  timeline entry listing changed fields.
- The 4-field fill-only logic and its glue-summary are deleted.

### 6. Computable deadline

`compute_deadline_at(deadline_date, deadline_time, deadline_timezone) -> aware datetime | None`

- No date → None. Time parsing: accept `17:00`, `17.00`, `5 PM`, `5:30pm`;
  unparseable/empty → 23:59:59. Timezone: IANA names + common abbreviations
  (CET, CEST, EET, EEST, GMT, UTC, BST, EST, EDT, PST, PDT…) via a small
  mapping; unparseable/empty → UTC.
- Set at creation, at Refresh from Zendesk, and backfilled in the data
  migration. Urgency math (sidebar facts `deadline.days_left`, any dashboard
  sorting) switches to `deadline_at`; the raw human-entered strings remain
  stored and displayed.

### 7. UI + facts updates

- **Active claims** = `status_category in (new, open, pending, hold)` —
  replaces the `['Received', 'Searching']` filters and per-status counts in
  apps/users/views.py (fixes problem 7). Manager dashboard counts become
  per-family (with per-status detail available later, in the one-screen round).
- **Badges**: color by `status_category` (5 stable colors), label = the real
  status name (templates/agent/dashboard.html:205,
  templates/manager/dashboard.html:188, claim_detail status card). The
  status-derived refund/dispute badges on claim_detail (lines 74-95) switch to
  record-based logic only.
- **Sidebar facts** (`build_claim_facts`): status verbatim + family;
  `next_update_due` suppressed when `status_category == 'solved'` (stop
  nagging finished cases). Sidebar app shell needs no change (it prints the
  status text it receives).
- claim_detail "AI summary" card shows `ai_summary` + "updated X ago" from
  `ai_summary_updated_at`.

### 8. Testing (TDD throughout)

- Resolver: cache hit / miss→fetch / unknown→refresh→fallback; cred-missing error.
- Webhook: 401 on missing/bad secret; create at investigation-initiated;
  status-change updates fields + timeline + summary; same-status no-op;
  non-claim ticket ignored; AI failure keeps old summary but stage still moves.
- Data migration mapping function (pure function, unit-tested).
- Deadline parser: table-driven cases (times, abbreviations, garbage, empties).
- Summary engine: mocked AIClient; PII names passed; failure → None.
- Refresh view: structured-overwrite vs LLM-fill-blanks policy.
- Dashboard queries: family-based counts include Object Found cases.
- Update existing tests that reference old status values.

### 9. Rollout

1. Pre-deploy (user, in this order):
   a. Fill SystemSettings Zendesk credentials (already pending — claim
      creation is broken without them; §2 also depends on them).
   b. Confirm the Zendesk webhook that posts to `zd/claim-webhook/` sends the
      `X-Webhook-Secret` header (Admin Center → Apps and integrations →
      Webhooks) — required once auth becomes mandatory.
   c. Confirm nothing targets `zd/status-webhook/` (it is being deleted).
2. Deploy = git push → Railway (migrations run on release; verify the release
   command runs `migrate` during implementation).
3. Post-deploy verification: flip a test ticket through several statuses →
   claim mirrors each, timeline grows, summary refreshes; check active counts.
4. **Thorough code review of the claims area** (user-requested) closes the
   round.

## Out of scope (deliberately, for later rounds)

- The management one-screen view (filter by family/status, issues queue) —
  next round, on top of this round's data.
- Tracking which client updates were actually SENT (needs comment-event
  webhooks; cadence stays age-based for now).
- Writing statuses back to Zendesk from LORA (mirror stays one-way).
- Auto-creating Refund records from "Refund Requested" status (old broken
  behavior not carried over; refunds enter via PayPal/manual paths).
- Institutions directory; email-triggered summary regeneration.
