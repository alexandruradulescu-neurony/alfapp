# Claim Risk Flag + Delta-Aware Timeline Summaries — Design

- **Date:** 2026-06-18
- **Status:** Approved design, pending spec review
- **Author:** brainstormed with Claude

## Problem

Real case (Zendesk #54281). A client lost a laptop, ALF filed reports, the item
was located — and the client then **called ALF a scam and demanded a refund**.
The case was closed "Solved." Reviewing the claim timeline, two failures stand
out:

1. **The negative signal is buried, not flagged.** Every AI summary *does*
   mention "the client called ALF a scam and requested a refund" — but each one
   *leads* with "The case is Solved, meaning it ended successfully," with the
   scam/refund line as a trailing subordinate clause. A manager scanning a list
   of "Solved" claims sees green and moves on. There is no structured signal to
   filter, badge, or alert on. The gap is **surfacing**, not capture.

2. **The timeline parrots itself, and can contradict reality.** Each
   `ClaimUpdateTimeline` entry regenerates a full standalone re-narration of the
   whole case, with slight drift each time. In #54281 the status even went
   **backwards** — `Solved → Investigation initiated → Solved` within 16
   minutes — and nothing flagged that regression. Worse, the "Investigation
   initiated" entry asserted *"have not yet filed loss reports"* when reports had
   been filed two days earlier: the status name's canned meaning overrode the
   actual facts.

## Goals

- **A.** A **sticky, acknowledgeable risk flag** on each claim, surfaced as a
  **badge + filter** in management triage, so adversarial signals (hostile
  client, refund demand, dispute risk, status regression) are guaranteed to be
  seen — never silently erased by a later cheerful summary.
- **B.** **Delta-aware timeline entries** — each timeline event describes what
  *changed*, anchored on the concrete event, instead of re-narrating the whole
  case.

## Non-goals / out of scope

- Active alerts (email/Slack/Zendesk note on a new signal). Considered and
  deferred — badge + filter first; alerts are a possible later phase.
- General CSAT / warm-cold sentiment scoring. This is a **risk/escalation**
  flag, not a satisfaction metric.
- Any client-facing change. Internal management visibility only.
- Changing the stored `claim.ai_summary` snapshot's purpose (it stays a full
  current-state blurb).

---

## Part A — Sticky claim risk flag

### Data model (new fields on `apps/claims/models.py::Claim`)

| Field | Type | Purpose |
|-------|------|---------|
| `risk_level` | choice: `none` / `watch` / `at_risk` | severity; drives the badge |
| `risk_reasons` | JSON list of tags | accumulating set (see taxonomy) |
| `risk_detail` | short text | latest one-line justification |
| `risk_first_flagged_at` | datetime, null | when it first went non-`none` |
| `risk_last_signal_at` | datetime, null | most recent contributing signal |
| `risk_acknowledged_at` | datetime, null | set when a user clears the badge |
| `risk_acknowledged_by` | FK user, null | who acknowledged |

**Reason taxonomy** (`risk_reasons` tags):
`hostile_language`, `refund_demanded`, `dispute_risk`, `status_regression`,
`negative_sentiment`.

**Severity mapping** (depends on both the reason *and its source*):
- "Hard" reasons: `refund_demanded`, `dispute_risk`, `hostile_language`,
  `status_regression`. "Soft" reason: `negative_sentiment`.
- `at_risk` — any hard reason that is **AI-corroborated** or **deterministic**
  (`status_regression`).
- `watch` — only soft reason(s), or only **uncorroborated keyword-booster** hits
  of a hard reason (a bare keyword match can be a false positive, e.g. "this
  isn't a scam").
- `none` — no reasons.

### Sticky semantics

- A detection pass (AI + keyword + regression) may only **add** reasons (set
  union) and **raise** `risk_level` to the max seen. It must **never** remove a
  reason or downgrade severity. This is the keystone: a later "Solved" cannot
  erase a raised flag.
- `risk_first_flagged_at` is set once (first transition out of `none`);
  `risk_last_signal_at` updates on every contributing signal; `risk_detail` is
  replaced with the latest justification.
- **Acknowledge** (a user action) sets `risk_acknowledged_at` / `_by`. This
  clears the *active* badge (the claim drops out of the "unacknowledged" filter)
  but leaves `risk_level` / `risk_reasons` / `risk_first_flagged_at` intact for
  audit.
- **Re-raise after ack:** if a *new* signal arrives after acknowledgement (a
  reason not already present, or `risk_last_signal_at` would advance on a fresh
  detection), clear `risk_acknowledged_at` / `_by` so the badge returns. An ack
  silences what was known *then*, not future bad news.

### Detection (Approach 1 — hybrid, one LLM pass)

Detection runs inside the existing summary generation pass (see Flow), so there
is **no extra LLM call**.

1. **AI (primary).** The briefing summary call returns a structured object that
   now includes a risk read. New schema (e.g. `apps/ai/schemas.py::ClaimBriefing`):
   ```
   { summary: str,
     risk_reasons: list[<taxonomy enum>],
     risk_severity: "none" | "watch" | "at_risk",
     risk_note: str }
   ```
   PII is tokenized on the way to the model and restored on return, exactly as
   the existing summary/categorizer calls already do (the note may contain the
   client name — fine for internal display; Zendesk/internal is inside the
   trust boundary).
2. **Keyword booster (deterministic safety net).** A case-insensitive scan of
   the ticket thread for **unambiguous** escalation terms: `scam`, `charge ?back`,
   `fraud`, `lawyer`, `attorney`, `BBB`. A hit forces at least `watch` and the
   matching reason (`dispute_risk` for chargeback, `hostile_language` for
   scam/fraud, etc.) even if the model under-read it.
   - Deliberately **excludes** domain-common words like `refund`, `dispute`, and
     `complaint`: this business describes a *non-refundable fee* on every claim
     and "dispute" is a routine PayPal term, so those would fire on nearly every
     case. Distinguishing "demanded a refund" from "agreed to the non-refundable
     fee" is left to the AI, which has the context to tell them apart.
3. **Status regression (deterministic).** Computed in the webhook mirror
   (`mirror_status_change`). To avoid false positives from the fuzzy ordering of
   open/pending/hold, we hard-flag only the unambiguous, high-signal case: the
   previous status category was **`solved`** (terminal) and the new category is
   **non-terminal** (a reopen). That trips `status_regression` + `at_risk`. (This
   is exactly the #54281 `Solved → Investigation initiated` jump.)

### Surfacing

- **Claim list:** a red "⚠ At risk" pill on rows where `risk_level == at_risk`
  and not acknowledged (reasons shown on hover/title). A **filter / tab "At risk
  (unacknowledged)"** = `risk_level != none AND risk_acknowledged_at IS NULL`.
- **Claim detail:** a banner showing reasons + `risk_detail` +
  `risk_first_flagged_at`, with an **Acknowledge** button.
- **Acknowledge action:** authenticated POST (single trusted-user model — no
  role gate), e.g. `POST /claims/<id>/acknowledge-risk/`, sets
  `risk_acknowledged_at` / `_by`.

---

## Part B — Delta-aware timeline

### The snapshot stays full

`claim.ai_summary` remains a full current-state summary (the "what is this claim
right now" blurb). Unchanged behavior — re-stating the whole picture is correct
for a snapshot.

### Timeline entries become deltas

`ClaimUpdateTimeline` rows (the log) change shape:

- **Status change** (webhook path) → **always** create an entry.
  - **Headline** = the deterministic transition, e.g. `Claim submitted → Solved`.
    A regression is rendered `Solved → Investigation initiated ⚠`.
  - **Body** = an LLM "what's new since the previous entry," given the *previous*
    entry's text as context and instructed to output only the delta. If nothing
    new beyond the transition → **"No new information."**
- **Manual refresh** ("update from Zendesk") →
  - If `refresh_claim_from_zendesk` reports **no changed fields** → **no entry**
    (suppressed). This is deterministic and reliable (the merge step already
    returns `updated_fields`).
  - If fields changed → an entry describing **only** the changed facts.

### Delta generation

A new briefing helper (e.g. `briefing.generate_timeline_delta(claim,
prev_entry_text, event)`) produces the body, separate from the snapshot
generator. The webhook + refresh paths call it; the snapshot generator is
untouched.

### Boilerplate-contradiction fix (in scope, cuttable)

The status name's canned meaning (`STATUS_VOCABULARY` in briefing.py) becomes
*context* rather than an assertion: the prompt instructs the model to use it to
interpret the stage but to **defer to the actual thread/claim facts**, so it
stops stating things like "not yet filed loss reports" on a case where reports
were filed.

---

## Flow / trigger points

Both features ride the existing triggers; one LLM pass per event yields summary
+ risk together.

1. **Status-change webhook** (`ZendeskClaimWebhookView` → `mirror_status_change`):
   deterministic regression check → summary+risk pass → update snapshot, write a
   timeline delta entry, union risk fields.
2. **Manual "refresh from Zendesk"** (`ClaimUpdateFromZendeskView` →
   `refresh_claim_from_zendesk`): merge fields → summary+risk pass → update
   snapshot, write a timeline delta entry **only if fields changed**, union risk
   fields.
3. **Acknowledge action:** clears the active badge.

Risk detection only re-evaluates at these moments — which are exactly the points
where new information enters the claim, so that is the intended cadence.

---

## Testing (strict TDD)

Behavior changes are written test-first (RED verified before GREEN). Key cases:

- **Risk detection:** scam/refund/chargeback in thread → correct reasons +
  severity; AI risk read mapped to fields; keyword booster forces `watch` when
  AI misses; `solved → non-terminal` trips `status_regression` + `at_risk`;
  `open → solved` (forward) does **not**.
- **Sticky:** a second pass with a *clean* read does not downgrade or drop
  reasons; severity only rises.
- **Acknowledge:** clears active badge (drops from filter), keeps reasons; a new
  signal after ack re-raises (clears `acknowledged_at`).
- **Surfacing:** the "at-risk unacknowledged" filter returns exactly the right
  set; badge renders.
- **Timeline:** status change always logs (transition headline; regression
  marked); no-op manual refresh writes **no** entry; manual refresh with changed
  fields writes an entry of only those facts; delta body says "No new
  information" when nothing changed beyond the transition.
- **Boilerplate fix:** status meaning no longer contradicts filed-reports facts.

---

## Build order

Two phases, each its own implementation plan / PR:

1. **Part A — the risk flag.** Higher value, directly fixes the reported problem.
2. **Part B — delta timeline** (incl. the boilerplate fix).

---

## Decisions confirmed at review (2026-06-18)

- **Re-raise-after-ack:** YES — a new signal arriving after an acknowledgement
  clears the acknowledgement so the badge returns.
- **Boilerplate-contradiction fix:** KEEP — included in Part B.
