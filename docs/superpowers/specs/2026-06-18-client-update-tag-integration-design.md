# Client-Update ↔ Zendesk-Tag Macro Integration — Design

- **Date:** 2026-06-18
- **Status:** Approved (owner confirmed 100%), spec of record
- **Author:** brainstormed with Claude

## Problem / goal
Client progress-updates have historically been done manually with Zendesk **macros** that (a) post an on-brand email and (b) stamp **tags** (`client_update_N`, removing `with_client_update`/`third_party_update`). LORA's automated cadence must integrate with that tag system so the two never collide, must send messages in the **same on-brand voice** (with the same field placeholders + a dynamic service period), and — system-wide — **must never sound AI-written**.

## A. Cadence ↔ macro ↔ tag mapping (the keystone)
The 5 macros are the FOLLOW-UP cadence (the initial "we've submitted" report is separate, on the Claim, and is NOT a `client_update_N`). The existing cadence at the default 30-day service produces exactly: `DAY_2, DAY_5, DAY_11, DAY_21, FINAL`. Map by **sequence position** (1-based ordinal in `cadence_plan`):

| Ordinal | Milestone (L=30) | Tag | Macro | Closes? |
|---|---|---|---|---|
| 1 | DAY_2 | `client_update_1` | Update – 2 Days | no |
| 2 | DAY_5 | `client_update_2` | Update – 5 Days | no |
| 3 | DAY_11 | `client_update_3` | Update – 11 Days | no |
| 4 | DAY_21 | `client_update_4` | Update – 21 Days | no |
| 5 | **FINAL** (≈day 30) | `client_update_5` | Update – 30 Days (final) | **no — tags only** |

**Extension (service_length_days > 30):** the existing tail emits `DAY_31, DAY_41 …` before FINAL. So ordinal 5 becomes a still-searching tail update (`client_update_5`, styled like DAY_21) and **FINAL shifts to ordinal 6** (`client_update_6`, the closer). This matches the owner's model exactly. The tag is therefore `client_update_{ordinal}` where ordinal = the milestone's 1-based index in `cadence_plan(claim)` — no new model field; a `tag_for_milestone(claim, milestone)` helper computes it.

## B. The day-30 (FINAL) closer — tags, NOT auto-close
The 30-day macro also sets status → `Closed - Object Not Found` and adds `item_not_found`, `30_days_reached`, `investigation_over`. **LORA will add those three terminal tags (plus `client_update_5/6`) and remove the attention pair, but will NOT change the ticket status** — an agent closes manually. (LORA today never writes Zendesk status; we keep it that way.)

## C. Tag ledger mechanic (read-before, write-after)
- **Read-before:** before LORA prepares/sends update N, it reads the ticket's current tags. If `client_update_{ordinal}` is **already present**, that update was already done (e.g. an agent ran the macro) → LORA marks the `ClientUpdate` row **SENT/SKIPPED (done)** and advances the cascade (`schedule_next`) WITHOUT posting. No duplicate.
- **Write-after:** when LORA itself sends update N, it **adds `client_update_{ordinal}`** and **removes `with_client_update` + `third_party_update`** (mirroring the macro). FINAL additionally adds the three terminal tags.
- **Attention-tag semantics (owner-confirmed):** `with_client_update` = set when the client emails us; `third_party_update` = set when a recovery center notifies us. Both are cleared by any update (the update addresses them). Clearing `with_client_update` is safe because if the client is hostile/refund/scam the cadence is already **paused** (the Phase-1 risk gate) — we never auto-update in those cases.

## D. Dynamic service period
Everywhere the macro copy says "30 days / 30th day / 30-day timeframe", LORA's templates substitute **`SystemSettings.service_length_days`** (default 30) so an extended period reads correctly.

## E. Field placeholders (match the macros)
LORA's messages fill the SAME fields the macros do, sourced at build time from the live ticket `custom_fields` (we fetch the ticket anyway) via the field-ID constants, falling back to claim attrs:

| Placeholder | Field ID | Source |
|---|---|---|
| first name | requester | **derive** from `Claim.client_name` (first token) — no first-name field exists |
| lost item | 11761123532444 | `Claim.object_description` (first line) |
| airport | **11761104069276** | ticket custom field (Airport) — *recon corrected the swap* |
| airline | **11761080032028** | ticket custom field (Airline) / `flight_data['airline']` |
| flight | 13737630819996 | ticket custom field (Flight #) / `flight_data['number']` |
| flight date/time | 13737598795292 | ticket custom field (Date & Time) — flight date, not the deadline |
| ALF report # | 11688794648732 | `Claim.alf_claim_id` |
| phone | 11761070082844 | `Claim.phone` (PII — masked before any LLM) |

A `macro_fields(claim, ticket_data) -> dict` helper extracts these; missing values degrade gracefully (omit the clause, never print an empty placeholder).

## F. Per-milestone messages (voice)
Replace the single shared `_no_news_template` with **per-milestone, macro-based templates** (rearranged/tidied versions of the owner's macro copy — keep substance + voice, improve flow), placeholdered (E) and period-dynamic (D):
- DAY_2, DAY_5, DAY_11, DAY_21 — distinct "still searching / our process" messages.
- A generic "still searching past N days" template for tail milestones (DAY_31+ when extended), styled on DAY_21.
- FINAL — the closing message (uses the final disclaimer).
Each may be lightly AI-polished (keep-every-fact + the global anti-AI rule); deterministic template is the always-available fallback. The reply-driven enrichment path (safe office replies → AI draft) stays, also under the anti-AI rule.

## G. Global "never sound AI-written" rule (system-wide)
A new `STYLE_RULE` appended in `apps/ai/prompt_fence.py::build_messages` (where `DEFENSE_PREAMBLE` is concatenated) so it rides **every** `AIClient.complete()` call (summaries, updates, drafts, dispute narratives, everything). Content: write like a human support agent — **no em-dashes (—) or en-dashes used as punctuation**, no AI tells (no "delve/tapestry/moreover/it's worth noting/in conclusion", no overusing "—", no robotic uniformity); plain, warm, concrete. Phrased to apply to **human-facing message text** so it's a harmless no-op for structured-extraction calls.

## New helpers to add
- `services.get_zendesk_ticket_tags(zd_ticket_id) -> list[str]` (read).
- `services.remove_zendesk_ticket_tags(zd_ticket_id, tags) -> bool` (DELETE /tags.json).
- `client_updates.tag_for_milestone(claim, milestone) -> str` (ordinal → `client_update_N`).
- `client_updates.macro_fields(claim, ticket_data) -> dict` (placeholder values).
- per-milestone template functions in `client_updates.py` (or a new `client_update_templates.py`).

## Build order (tasks)
1. **T1 — global anti-AI `STYLE_RULE`** in `prompt_fence` (foundational; affects all output; verify full suite).
2. **T2 — Zendesk tag helpers** (read + remove) in `services.py`.
3. **T3 — `macro_fields` + first-name derivation** (placeholder extraction from ticket/claim).
4. **T4 — per-milestone macro templates** (D + E + voice) wired into `prepare_follow_up`.
5. **T5 — tag ledger** (`tag_for_milestone`; read-before-skip; write-after add/remove; FINAL terminal tags, no close).

## Out of scope / confirmed
- LORA never changes Zendesk **status** (no auto-close).
- The 31-day macro text isn't provided; the tail/extension message is modeled on DAY_21 + dynamic period (owner-confirmed "looks like client_update_4").
- Risk pause (Phase 1) and the initial report (already shipped) are unchanged; this builds on them.
