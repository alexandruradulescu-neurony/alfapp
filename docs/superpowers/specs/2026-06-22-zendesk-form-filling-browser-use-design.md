# Zendesk "Form Filling" tab — Browser Use Cloud integration

**Status:** Design / awaiting review
**Date:** 2026-06-22
**Author:** brainstormed with the user

## Summary

Add a **Form filling** tab to the LORA Zendesk sidebar app. On a ticket that has a
linked LORA claim, the human agent pastes a lost-item **report form URL**, clicks
**Fill form**, and an AI browser agent (Browser Use Cloud) opens the form in a cloud
browser and fills it from the claim's data. The agent reviews the filled form (as a
screenshot in the tab, or by watching the live browser in a pop-out tab), clicks
**Approve & submit**, and the bot submits and captures a confirmation screenshot.
Optionally the confirmation screenshot is posted to the ticket as an internal note.

This is the "browser automation" item previously parked as a roadmap *maybe*
([[project_roadmap]]). It fits LORA's role as the AI gateway + action engine
([[project_lora_architecture_principle]]) and the existing button-driven sidebar
pattern ([[project_zendesk_sidebar_app]]).

## Goals

- Let an agent fill an institution's lost-item report form from a LORA claim with
  near-zero typing — paste URL, review, approve.
- Keep a human in the loop: the bot **fills but does not submit** until the agent
  explicitly approves.
- Keep client PII out of the AI model (consistent with [[project_llm_trust_boundary]]).
- Reuse LORA's existing patterns: sidebar tab → LORA endpoint → external service;
  secret/API-key in `SystemSettings`; feature-flagged off by default.

## Non-goals (this iteration)

- Self-hosting the browser agent (cloud only for now; revisit if the new-processor
  PII posture is unacceptable).
- Automatic captcha solving (fallback = human takes over in the live view).
- A library of per-institution form templates / deterministic replay (future).
- Filling forms for tickets with **no** linked LORA claim (the tab requires a claim).

## Decisions made during brainstorming

1. **Autonomy = fill → human approves → submit** (not fill-only, not auto-submit).
   The bot fills and pauses; the agent approves; then the bot submits.
2. **Data source = auto-fill from the linked LORA claim.** The agent does not type
   field values; LORA supplies the claim's data.
3. **Hosting = Browser Use Cloud** (HTTP API + key), not self-hosted. No local
   browser/Playwright install; LORA backend calls the cloud over HTTPS.
4. **Review surface = screenshot inside the tab** (fits the narrow ~320px sidebar) +
   an **Open live view** button that pops the interactive cloud browser into a full
   browser tab for watching or manual takeover.
5. **PII = domain-scoped "secrets".** Claim data is passed as Browser Use *secrets*
   so the underlying LLM never sees real values; the browser fills them directly.

## User flow

1. Agent opens the **Form filling** tab on a ticket with a linked claim.
2. Pastes the institution form URL. (Optional: tick **Post screenshot to ticket**.)
3. Clicks **Fill form**.
4. LORA starts a Browser Use session: *"fill this form with the provided details,
   do NOT submit"*, claim data as domain-scoped secrets, recording on.
5. The tab shows progress; when the fill finishes, it shows a **screenshot of the
   filled form** with **Approve & submit** / **Cancel**. An **Open live view** button
   is available throughout (opens the interactive browser in a new tab — used to
   watch, or to take over a login/2FA/captcha by hand).
6. Agent reviews → **Approve & submit**.
7. LORA sends a follow-up *"now submit"* to the same live session; the bot submits
   and the **confirmation screenshot** is captured.
8. If the box was ticked, LORA posts the confirmation screenshot to the ticket as an
   internal note (renders as an image — see [[project_email_system]] note rendering).
9. **Cancel** stops the session without submitting.

## Architecture

### Components

- **Browser Use Cloud** — hosted browser-agent service (external). Talked to over
  HTTPS with an API key.
- **`apps/integrations/browser_use.py`** (new service module) — thin wrapper over the
  cloud API: `start_form_fill(url, secrets, allowed_domains)`, `get_session(id)`,
  `latest_screenshot(id)`, `submit_session(id)`, `stop_session(id)`. Raw HTTP via the
  existing `requests` dependency (no new package), mirroring how the Anthropic path is
  called ([[project_ai_provider_split]]).
- **LORA sidebar endpoints** (new, sidebar-secret authed like the other `zd/*`
  endpoints): start a fill, poll status + screenshot, approve/submit, cancel.
- **Zendesk app** — new **Form filling** tab in `zendesk_app/assets/iframe.html` +
  `zendesk_app/assets/app.js` (URL input, Fill/Approve/Cancel buttons, screenshot
  area, Open-live-view button, post-screenshot checkbox). Plain JS, polling — no
  Alpine/eval (prod CSP), consistent with the existing tabs.
- **`SystemSettings`** — `browser_use_api_key` (secure) + `form_filling_enabled`
  (bool, default False) + (optional) `browser_use_model` (default the recommended
  Claude Sonnet model).

### Data flow

```
Zendesk tab ──POST zd/form-fill/start {ticket_id, url, post_screenshot}──▶ LORA
  LORA: load claim → build secrets dict → Browser Use POST /sessions
        (task="fill, don't submit", secrets, allowed_domains=[form host])
  ◀── {session_id, live_url} ── LORA ◀── Browser Use
Zendesk tab ──poll zd/form-fill/status {session_id}──▶ LORA ──GET /sessions/{id}──▶ BU
  ◀── {status, screenshot (proxied), live_url} ──
[agent clicks Approve]
Zendesk tab ──POST zd/form-fill/submit {session_id}──▶ LORA
  LORA: Browser Use follow-up task "now submit" on same session_id
  on finish: capture confirmation screenshot → (if opted) post_zendesk_comment(html_body=<img>)
[agent clicks Cancel] ──POST zd/form-fill/cancel──▶ LORA ── BU stop(session)
```

## Browser Use Cloud API usage (from docs.browser-use.com, v3)

- **Auth:** header `X-Browser-Use-API-Key: bu_…`.
- **Create session:** `POST https://api.browser-use.com/api/v3/sessions` with
  `{ task, secrets: { "<form-host>": "key:value;…" }, allowed_domains: ["<form-host>"],
  enable_recording: true, model: "<claude-sonnet>" }`. Returns `id` + `live_url`
  (interactive, embeddable or open-in-tab).
- **Status/result:** `GET /api/v3/sessions/{id}` → `status`
  (running/idle/stopped/error/timed_out) + `output`. Messages stream
  (`GET /api/v3/sessions/{id}/messages`) carries `screenshot_url` values.
- **Follow-up (the approve→submit step):** post a new task to the **same** session
  (`run(task, session_id=…)`); browser state (page, cookies) carries over.
- **Stop/cancel:** `stop(session_id, strategy="task"|"session")`.
- **Secrets:** domain-scoped; the LLM never sees the values, filled programmatically,
  encrypted at rest.
- **Timeouts:** ~15 min inactivity, 4 h max; extend with a lightweight follow-up.
- **Model:** selectable; default to the recommended Claude Sonnet tier.

> Implementation note: the docs are slightly version-ambiguous on **intermediate
> screenshot retrieval** (a v1 `GET /api/v1/task/{id}/screenshots` exists alongside the
> v3 `messages` `screenshot_url`). The plan will confirm the exact call against a live
> key and pin one path. Fallback if neither gives a clean still: render the `live_url`
> for review instead of a screenshot.

## LORA backend

- **`apps/integrations/browser_use.py`** — wrapper functions above; all network in one
  place; raises a typed error on failure; never leaks the API key.
- **Endpoints** (in the sidebar views module, `verify_webhook_secret`-authed like the
  other `zd/*` calls):
  - `POST /api/integrations/zd/form-fill/start` → `{ticket_id, url, post_screenshot}`
    → resolves claim by ticket, builds secrets, starts the session, returns
    `{session_id, live_url, status}`. 400 if no linked claim / feature off.
  - `POST /api/integrations/zd/form-fill/status` → `{session_id}` →
    `{status, screenshot (LORA-proxied data URL), live_url}`.
  - `POST /api/integrations/zd/form-fill/submit` → `{session_id, ticket_id, post_screenshot}`
    → follow-up "submit" task, captures confirmation screenshot, optionally posts the
    note, returns `{status, screenshot}`.
  - `POST /api/integrations/zd/form-fill/cancel` → `{session_id}` → stop session.
- **Screenshot proxying:** LORA fetches the screenshot from Browser Use and returns it
  to the tab (as a data URL or via a LORA-served URL) so the sidebar never has to load
  cross-origin images (avoids touching the app's CSP/domain whitelist). The **live
  view** is opened with `window.open(live_url)` in a new tab — never embedded — so no
  whitelist/CSP change is needed.

## Data mapping (claim → form)

LORA passes the claim's relevant fields to Browser Use as `secrets` (values hidden
from the LLM): client name, email, phone (if present), item description, lost
location, flight details, loss date, and the ALF claim reference. The agent is
AI-driven, so it maps these to the form's own fields by reading the field labels — no
rigid per-form field map is required. The exact claim field set is finalized in the
plan against the `Claim` model.

## PII / trust boundary

- Client data is sent as **domain-scoped secrets**, so the AI model never sees real
  values — consistent with [[project_llm_trust_boundary]] (the boundary is the LLM).
- **New decision to record:** Browser Use Cloud (the company) becomes a new data
  processor that receives the claim data (encrypted at rest) to type it into the form.
  This is the same *category* of trust already extended to Zendesk/PayPal, and the data
  is being sent to the institution anyway. Accepted for the cloud approach; self-host
  is the escape hatch if that changes. Update [[project_llm_trust_boundary]] and
  [[project_ai_provider_split]] notes when shipped.

## Error handling & edge cases

- **No linked claim / feature flag off:** endpoint returns a clear error; the tab shows
  "Link a LORA claim to use form filling" / "Form filling is turned off in Settings".
- **Captcha / login / 2FA:** the bot can't always pass these. The tab surfaces **Open
  live view** so the agent can take over by hand, then resume/approve.
- **Session inactivity timeout (~15 min):** if the agent takes too long to approve, the
  session naps. LORA either sends a keep-alive follow-up while the tab is open, or the
  status call reports "expired — re-run". Decision in the plan; simplest is re-run.
- **"Don't submit" not honored:** rare, but the approval gate is the safety net — a
  stray early submit shows up in the screenshot; we surface it and do not re-submit.
- **Submit fails / form errors:** confirmation step reports failure; nothing is posted
  to the ticket; agent can open live view to finish manually.
- **Network/API errors:** typed error → friendly message in the tab (same diagnose()
  pattern the app already uses).

## Screenshot → Zendesk note

Reuses the internal-note image rendering shipped in PR #85 ([[project_email_system]]):
the confirmation screenshot is posted via `post_zendesk_comment(..., html_body=<img …>)`
(sanitized), so it renders inline on the ticket. Only posted when the agent ticked the
box.

## Testing

- **Service wrapper:** unit tests with the HTTP layer mocked — request shape (task,
  secrets, allowed_domains), status parsing, submit follow-up, stop, error mapping.
- **Endpoints:** auth required (sidebar secret); no-claim → error; feature-flag off →
  error; start returns session+live_url; submit triggers follow-up + optional note;
  cancel stops. Browser Use calls mocked.
- **No live key in CI.** A separate, manual smoke test against one real form (with a
  real key) is run once during the plan to pin the screenshot-retrieval path.
- Follows [[feedback_strict_tdd]] for the wrapper/endpoint logic.

## Rollout / deploy

- Backend (service + endpoints + settings/migration) deploys via **Railway** (git push).
- The sidebar tab (`iframe.html` + `app.js`) needs a **`zcli apps:update`** push to go
  live — the user's manual step ([[project_zendesk_sidebar_app]]).
- Ships **off by default**; the user adds the Browser Use API key in Settings and flips
  `form_filling_enabled` when ready. Recommend proving it on one or two known forms
  (e.g. nettracer / an airline lost-item form) with the agent watching the live view
  before wider use.

## Open questions (to confirm during planning)

1. Exact intermediate-screenshot retrieval path (v1 screenshots endpoint vs v3 message
   `screenshot_url`) — pin against a live key.
2. Keep-alive vs re-run for the 15-minute approval window.
3. Whether to use webhooks (`agent.task.status_update`) instead of tab polling later
   (polling is fine for v1; webhooks are an optimization).
4. Final claim → secrets field list against the `Claim` model.

## Out of scope / future

- Self-hosted browser agent on Railway.
- Per-institution form templates + deterministic replay (cheaper reruns).
- Automatic captcha solving.
- Bulk / queued form filling across many claims.
