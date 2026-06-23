# Form-fill: structured context + per-site playbooks (with AI auto-generate)

**Date:** 2026-06-23
**Status:** Approved (brainstorm) — pending implementation plan
**Builds on:** the Form filling feature (PRs #88/#94/#95/#96); the LLM trust boundary; the AI provider split (DeepSeek default).

## Problem

At fill time, `build_agent_context` sends Browser Use the **entire masked ticket thread** plus the full `ALF_BUSINESS_CONTEXT` summarization preamble. This is:

- **Noisy and expensive** — a lost-item form needs ~12 facts; we hand over the whole case file (call-recording summaries, internal notes, ALF's own template emails, institution receipts, status scribbles). That inflates Browser Use token cost and gives the agent more to misread.
- **Lossy** — facts that arrived *later* in the case (baggage tag, booking confirmation) are buried, and number-shaped IDs are masked as `<PHONE_…>`, so the agent literally cannot enter the real baggage tag (the "phone problem").

## Goals

1. Send Browser Use a small, clean, **structured form profile** instead of the raw case.
2. Recover the real IDs that are masked in the thread (baggage tag, booking confirmation) and deliver them via the secret channel.
3. **Per-form-platform playbooks** stored in the DB and edited in a backend page (no deploy) that inject site-specific instructions into the agent's brief.
4. An **AI "Improve from recent runs"** button that drafts playbook updates from recorded run summaries; the human approves/saves.

## Non-goals (v1)

- Deep step-by-step run capture — we learn from the agent's **end-of-run summary**, which already reports what it filled and where it stuck. Add deeper capture later only if summaries fall short.
- Structured per-field rule rows in the playbook — free-text instructions only for now.
- Auto-applying AI suggestions — always human-approved.

## Trust boundary (unchanged)

Real PII never reaches the Browser Use LLM. The structuring LLM sees only masked text. Masked categories (names, emails, phones, ALF-IDs, flights — plus recovered IDs like the baggage tag) are delivered to the form via Browser Use **secrets** (typed verbatim, never shown to the model). Non-PII facts (item type, airport, airline, date, state/country) MAY be shown to the agent so it can choose dropdowns correctly.

## Components

### 1. Form profile builder — `build_form_profile(claim, ticket_data)`

- New module `apps/integrations/form_profile.py`.
- One AIClient **structured-output** call (DeepSeek by default), using the existing **tokenize → untokenize** machinery (as the dispute/summary extraction already does): the model sees masked case text and returns a Pydantic `FormProfile`; every string field is then **untokenized server-side**, so real values (including the baggage tag and marks like "Bronach") are recovered without the model ever seeing them.
- `FormProfile` schema (all fields optional):
  - **claimant:** `first_name, last_name, email_alias, phone`
  - **item:** `item_type, brand, colour, identifying_marks, item_description` (full clean description)
  - **loss:** `airport, airline, flight, lost_date, where_lost, how_lost`
  - **ids:** `baggage_tag, booking_confirmation, claim_ref`
  - **address:** `street, city, state, zip, country`
- **Secret vs. visible split (the key rule):**
  - **Secrets** (masked categories / free-text fields, never shown to the LLM): first/last name, email alias, phone, street, flight, `item_description`, `how_lost`, baggage tag, booking confirmation, claim ref → `x_*` keys with real values.
  - **Visible facts** (non-PII, for dropdowns + understanding): item type, brand, colour, airport, airline, lost date, where-lost, city, state, zip, country → listed in the brief as "known facts" so the agent can pick the right dropdown options (a secret placeholder cannot be *selected* from a dropdown — only typed — so dropdown values must be visible).
- **Caching:** persist on the Claim (`form_profile` JSONField + `form_profile_generated_at`); reuse on retries; regenerate when missing/stale or when forced. Avoids re-paying per retry.
- **Fallback:** if the structuring call fails, fall back to today's behaviour (secrets straight from the Claim fields, no thread). Never block a fill.

### 2. `FormPlaybook` model + backend page

- Model `apps/integrations/models.FormPlaybook`: `domain` (unique, lowercased host), `label`, `instructions` (TextField), `enabled` (bool, default True), `created_at`, `updated_at`. New `integrations` migration.
- Backend **Form playbooks** page (manager area, styled like Settings): list all playbooks; create/edit (domain, label, instructions, enabled); delete. Same auth as other manager pages.
- Matched at fill time by the form URL's host (`form_host`); if an enabled playbook exists, inject its `instructions`; otherwise proceed generically.

### 3. AI auto-generate — "Improve from recent runs"

- Each fill already records `FormFill.result_output` (the agent's end-of-run summary). For a given domain, gather the recent FormFills' summaries.
- Button on the playbook edit page → `suggest_playbook_instructions(playbook, recent_summaries)` → AIClient call (DeepSeek): input = current instructions + recent run summaries (tokenized for safety); output = a proposed revised instructions text.
- The draft loads into the instructions box (side-by-side or inline) for the user to edit and **Save**. Nothing auto-applies.
- No runs yet, or the call fails → friendly message, no change.

### 4. Browser Use brief — `build_fill_task` rewrite

- Replaces the thread + preamble. The new brief is:
  1. Role + form URL.
  2. **Known facts** (visible, non-PII): e.g. "Item type: Suitcase. Airport: Newark Liberty / EWR. Airline: Aer Lingus. Date of loss: 06/20/2026. State: IL. Country: US. Where lost: in transfer." — used to choose dropdowns.
  3. **Secret keys to type** (the `x_*` list with labels), now including `x_baggage_tag`, `x_booking_ref`, `x_item_description`.
  4. **Site playbook instructions** (from `FormPlaybook`).
  5. The existing generic rules (never type a `<…>` token; don't invent dropdown values; skip a fiddly control after two tries; do NOT submit).
- `build_form_secrets` builds the `x_*` dict from the profile (real values), falling back to Claim fields when there is no profile.

## Data flow

fill start → load/generate `FormProfile` (cached on the claim) → split into secrets + visible facts → look up `FormPlaybook` by domain → `build_fill_task` (known facts + secret labels + playbook + generic rules) → Browser Use session (secrets channel carries the real values). Run finishes → `result_output` stored → later, "Improve from recent runs" drafts playbook updates from those summaries.

## Error handling

- Structuring AI fails/times out → fall back to Claim-field secrets (no visible-facts enrichment); log; the fill still proceeds.
- Untokenize miss (a token with no mapping) → leave that field empty; never put a `<…>` token into a secret.
- No playbook for a domain → proceed with the generic rules.
- Auto-generate AI fails or there are no runs → message; no change to saved instructions.

## Testing

- `build_form_profile`: masked input → profile with **real** values after untokenize (baggage tag recovered, marks recovered); the secret/visible split is correct; real name/email never appear in the visible-facts text; fallback path on AI failure.
- `build_form_secrets` from a profile: real values; a `<…>` token never leaks into a secret.
- `build_fill_task`: includes known facts, secret labels, and playbook instructions; excludes the thread/preamble; keeps the safety rules (no tokens, no invented dropdowns, no submit).
- `FormPlaybook`: domain match by host; enabled flag respected; CRUD page (auth + save round-trip).
- Auto-generate: drafts from recent summaries; never auto-saves; no-runs and failure handled.

## Rollout / build order

A) profile pipeline + `build_fill_task` rewrite (immediately fixes the junk + the phone problem) → B) `FormPlaybook` model + backend page + fill-time injection → C) the AI auto-generate button. Backend-only except the new manager page; no Zendesk app push. Stays behind the existing `form_filling_enabled` flag.
