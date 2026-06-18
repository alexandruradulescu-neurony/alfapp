# Front-end redesign — pilot: the single-claim screen

**Date:** 2026-06-18
**Status:** Design approved, pending spec review
**Topic:** Declutter + restyle the core working screens, introduce HTMX + Alpine for in-place updates. Pilot one screen, then roll the pattern out.

---

## Plain-English summary

The core working screens (claims, single claim, emails, disputes, single dispute) feel cluttered, dated, clunky, and hard to scan. We rebuild them so they show only what matters, look calmer, and update in place without full-page reloads.

We do **one screen first** — the single-claim screen, the densest and most-used — fully: declutter it, restyle it, and make it snappy. The reusable parts that fall out of doing it for real (a refreshed page frame, a small set of shared components, the in-place-update wiring) then get applied to the other four screens, each in its own follow-up pass.

This is a presentation-and-interaction change. The buttons do the same things they do today, hit the same endpoints, and produce the same outcomes. No business logic changes.

---

## Goals

- Remove on-screen clutter: fewer buttons, fewer always-visible fields, one status instead of five or six, no raw technical IDs or JSON dumps in the main view.
- Make interactions feel app-like: sending/regenerating/skipping an update, resolving an email, checking mail update only the affected piece — no whole-page reload or scroll-to-top.
- Refine the flat visual styling: a modest, centralized restyle of the shared tokens (spacing, color/contrast, typography, card style). The surfaces are already flat — this polishes that; it does not reintroduce glass.
- Produce a reusable foundation (base layout, components, HTMX/Alpine wiring) so the other four screens are fast to convert.

## Non-goals

- No change to business logic, endpoint contracts, or what any action does.
- Not redesigning dashboard, settings, users, refunds, or AI-agent/chat screens in this effort.
- Not adopting a heavy front-end framework (React/Vue). HTMX + Alpine only.
- Not a new brand/color system — we keep the existing Tailwind + DaisyUI `lora` theme, just calmed down.

---

## Scope

**Pilot (this spec):** the single-claim screen.
- Live route: `/agent/claims/<claim_id>/`, view `agent_claim_detail` in `apps/users/views.py:410`, template `templates/agent/claim_detail.html` (~1,164 lines, ~240 of which are inline JavaScript). Confirmed live; no routed manager equivalent.

**Rollout (later, separate passes, not this spec):** claims list, emails, disputes list, single dispute — reusing the pilot's foundation.

---

## Decisions (approved)

| Question | Decision |
|---|---|
| Execution strategy | Pilot one screen fully, then roll out |
| Adopt HTMX + Alpine | Yes |
| Layout model | Two columns: left = the work, right = reference facts |
| Reference cards | Collapse all by default; expand on click |
| Visual style | App is already flat (since `53a71ba`). Keep flat; refine the shared flat tokens centrally alongside the layout work |
| HTMX/Alpine delivery | Download + commit into static files; no `npm install`, nothing fetched at runtime |
| Behavior | No business-logic changes; existing test suite stays green |

---

## Target screen — new structure

The current screen stacks eight sections (header, risk alert, cadence prompt, main update, follow-ups, left sidebar of 5 always-open cards, right sidebar of 4 cards, refund modal) with ~25 interactive controls. The redesign reorganizes the same information into a compact header plus two columns.

### Header bar (compact)
- Back chevron · client name · object lost · claim number.
- **One** status pill (the primary claim status). The Case / Refund / Dispute sub-states move into the collapsible "Status" reference card on the right.
- Only **urgent** chips appear inline: risk flag (if active), "N replies need action" (if any). Nothing else.
- Everyday actions top-right: `Send update`, `Check email`.
- Rare/dangerous actions behind a `···` menu: refresh-from-Zendesk, mark-as-disputed, delete, open-in-Zendesk.

### Left column — "the work"
1. **Client communication** — the main "what we did" update and the day-2/5/11/21 follow-ups, merged into one chronological list. Surfaces what is *due* or *sent*; older sent items are quiet/condensed. Per item: status (sent + timestamp, drafted, due, skipped) and its actions (review/send, regenerate, skip). The "start client updates" prompt appears here when no cadence has begun.
2. **Institution replies** — incoming emails from lost-&-found offices, grouped per office, with any reply needing action floated to the top. Handled replies are collapsed behind a "N handled replies" toggle. (Per-office grouping matters: a claim is sent to many offices at once, and "not found" from one office is not the claim outcome.)

### Right column — "the facts" (all collapsed by default)
Each card is a one-line summary; click to expand. Order:
- **Status** — case / refund / dispute sub-states, Zendesk ticket link.
- **Client** — name, emails, phone, addresses, email alias.
- **Case facts** — object, where/when lost, incident details, deadline, price paid, tracking, created/updated.
- **Flight** — flight number + verification badge; legs (route, terminal, gate, belt) inside the expanded view.
- **Refunds & evidence** — refund history + evidence thumbnails and upload, combined.

### Decluttering rules applied
- One primary status badge in the header; sub-states demoted into the Status card.
- Technical IDs (Zendesk ticket #, PayPal refund ID) and raw JSON (`changes_summary`) are not shown in the main view — they live inside expanded reference cards or are dropped.
- Flight terminal/gate/belt shown only inside the expanded Flight card, not by default.
- The duplicate "Grant refund" button collapses to one entry point (in the Refunds card / refund modal).

---

## Technical approach

### HTMX + Alpine delivery
- Download pinned versions of `htmx.min.js` and `alpine.min.js`, commit them under `static/js/vendor/`, and load them from `base.html` via `{% static %}`. No `npm install`, no CDN at runtime. (Bootstrap Icons / Inter remain as they are for now.)
- CSRF: HTMX sends the token via an `X-CSRFToken` header configured once globally (using the `csrf-token` meta tag already in `base.html`).

### In-place updates (the "snappy" part)
Principle: **each action returns the HTML for just the piece it changed, and the page swaps that piece** (`hx-post` + `hx-target` + `hx-swap`), instead of redirecting or reloading.

- The template-rendering action routes (`client_followup_send/prepare/skip`, `claim_client_report_send/generate`, `client_updates_start`, `claim_acknowledge_risk`) branch on the `HX-Request` header: return the updated fragment for HTMX calls, keep the existing full-page redirect as the no-JS fallback.
- The JSON API endpoints that currently drive a reload (`check-email`, `update-from-zendesk`, `email-logs/<id>/resolve`, `refunds/issue`, claim delete) are driven by HTMX and either (a) get a thin template route that renders the affected fragment, or (b) return JSON that a tiny handler turns into a fragment swap + toast. The underlying JSON API stays available for other callers.
- Toasts (success/error feedback) become an HTMX-triggered shared partial instead of the hand-rolled `showToast` function.

### What happens to the ~240 lines of inline JS
Mostly removed and replaced:
- AJAX-then-reload handlers (`updateFromZendesk`, `checkEmail`, `deleteClaim`, `resolveEmail`, refund submit) → HTMX requests with fragment swaps.
- Toggles, the `···` menu, expand/collapse of reference cards, and refund modal open/close → small Alpine attributes in the HTML.
- `getCsrfToken` → global HTMX CSRF header config.

### Shared components produced (the reusable foundation)
- Updated `base.html`: lighter-glass tokens, HTMX + Alpine loaded, global CSRF + toast container, reconciled navigation.
- Partial templates (e.g. `templates/partials/`): collapsible reference card, communication-list item, institution-reply item, toast, refund modal.
- Documented HTMX fragment pattern and Alpine usage conventions for the rollout screens.

---

## Visual: refine the flat styling

**Current state (corrected):** the app is already flat. Commit `53a71ba` (design refresh wave 1, 2026-06-14) turned `body.mesh-bg` into a plain `#f6f7fb` background and `.glass-panel` into solid white + a thin border — no mesh, no frost, no blur. The class names `mesh-bg` / `glass-panel` are vestigial; nothing glassy renders. Waves 2–3 applied consistent flat cards/pills app-wide (presentation-only — they did not declutter or restructure, which is why the density problems remain).

**Decision:** keep flat, and *refine* the flat look as part of this work — a modest, centralized restyle of the shared visual tokens (spacing rhythm, color/contrast, typography scale, card and pill style) in `static/src/css/tailwind.css`. Done once so every screen inherits it; no per-screen fiddling, no reintroduction of glass.

- Build workflow unchanged: edit `static/src/css/tailwind.css` (and/or templates) → `npm run build` → commit the compiled `static/css/tailwind.css`. Deploy does not rebuild CSS.
- The refined tokens land with the pilot and are reused by every rollout screen.
- Optional cleanup: rename the vestigial `mesh-bg` / `glass-panel` classes to honest names while we're in here (low priority; cosmetic, touches every template that references them).

---

## Cleanup carried along

- **Nav cross-wiring:** sidebar "Claims" points at the old `manager_claims` list, but a claim row opens `agent_claim_detail`. Reconcile to one consistent claims list → detail path (leftover from the removed manager/agent role split).
- Note any now-dead duplicate templates (`templates/manager/` vs `templates/agent/`) encountered while reconciling, but defer deleting non-pilot duplicates to their own rollout passes.

---

## Behavior preservation & testing

- No endpoint contract changes; every action produces the same outcome as today.
- The existing pytest suite (baseline ~1,085 passing) must stay green. Run with `.venv/bin/python -m pytest ... -o addopts=""`; sqlite tests, so avoid heavy parallelism.
- Add tests for any new fragment-rendering branches: assert that an `HX-Request` call returns the expected partial and that the non-HTMX path still redirects.
- Manual verification: load the rebuilt screen, exercise send/regenerate/skip/resolve/check-email/refund, confirm each updates in place with a toast and no full reload, and confirm the no-JS fallback still works.

---

## Risks / watch-items

- Refining the shared visual tokens risks drift across screens; mitigated by changing them once centrally and reusing them. Keep token changes modest in the pilot — the layout/declutter work is the bigger lever for "dated/messy."
- Fragment endpoints must keep the full-page fallback working so the screen degrades gracefully without JS.
- The single-claim view assembles a lot of context; splitting the template into partials must not change which data each section receives.

---

## Rollout (after pilot, not this spec)

Apply the foundation to: claims list → emails → disputes list → single dispute. Each is its own declutter + restyle + HTMX pass, reusing the components and patterns proven here. Consolidate `manager/` vs `agent/` duplicates as each screen is converted.
