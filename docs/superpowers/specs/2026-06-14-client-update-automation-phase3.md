# Client update automation — Phase 3 (autonomous runner) + configurable service length

Status: APPROVED, ready to build (2026-06-14). User confirmed "the system is perfect."
Builds on Phase 1 (cadence engine + LORA claim-page surface, commit 588e5e9) and
Phase 2 (Zendesk sidebar "Updates" tab + /zd/updates/ endpoint, commit 0b4b4d8) and
the "Start client updates" opt-in (commit 073cb66).

## What's already built (Phases 1–2)
- `ClientUpdate` model (apps/communications/models.py): claim FK `follow_up_updates`,
  milestone, due_at, state SCHEDULED→DRAFTED→SENT/SKIPPED, draft_body, has_news, sent_at.
- `apps/communications/client_updates.py`: schedule_follow_ups (currently PRE-CREATES all
  four), prepare_follow_up (reads office replies since last update → AI draft via
  FOLLOWUP_SYSTEM_PROMPT [multi-office rule], template fallback), send_follow_up (PUBLIC
  Zendesk reply), skip_follow_up, cancel_open_follow_ups, start_client_updates.
- Initial "what we did" message: Claim.client_report_draft / client_report_sent_at +
  apps/communications/client_report.py (template + optional AI polish, never promises recovery).
- Status-mirror webhook (apps/integrations/views.py `_handle_status_change`) drafts the
  initial + schedules follow-ups when entering the trigger status; cancels on solved.
- Sidebar tab (zendesk_app/assets/iframe.html + app.js) + `/zd/updates/` endpoint
  (ZendeskClientUpdatesView) with prepare/send/skip/start. Hybrid: agent clicks Prepare on a
  due one; agent approves every send.
- Trigger config: SystemSettings.client_report_trigger_status (currently a NAME string — WRONG, see below).

## Phase 3 — what to build

### A. Trigger by status ID, not name (CORRECTION)
- The webhook already receives the Zendesk custom-status **ID** (`custom_status_id`) and
  resolves it via `resolve_custom_status` (apps/integrations/services.py; cache key
  `zd_custom_statuses_v1`); there's a hardcoded `INVESTIGATION_STATUS_ID` in views.py as precedent.
- Change the trigger to compare the incoming `custom_status_id` against a configured
  **submitted-status ID** (repurpose/rename SystemSettings.client_report_trigger_status to hold
  the ID, or add client_report_trigger_status_id). On go-live the user sets the correct ID.
  Consider showing the available custom statuses + IDs on the Settings page (via
  _fetch_custom_statuses) so the right one is easy to pick.

### B. Cascade scheduling (CHANGE from Phase 1's pre-create-all-four)
- On submission: generate the initial report + schedule ONLY the next update (the +48h one).
- When an update RUNS (sent, by agent or autonomously): schedule the NEXT milestone, then.
- A void event simply means the next link is never created. Refactor schedule_follow_ups →
  a `schedule_next(claim)` that creates only the next due-but-uncreated milestone.
- Flag OFF still SCHEDULES the next step (so an agent can prepare it manually) — it just
  won't auto-send. (Confirmed answer #2.)

### C. The cadence (with CONFIGURABLE service length)
- Fixed early milestones (from the SUBMISSION moment): **day 2, 5, 11, 21**.
- Tail milestones (from submission): **31, 41, 51, …** — every +10 days starting at 31, while
  `day < service_length_days`.
- FINAL email at **service_length_days** measured from **claim/ticket creation** (confirmed
  answer #1), sent ONLY if the object was not found; voided if the claim closes/finds earlier.
- Examples (service_length_days L): L=30 → final@30 only. L=35 → 31, final@35. L=40 → 31,
  final@40. L=45 → 31, 41, final@45. L=55 → 31, 41, 51, final@55.
- Early/tail anchored to submission; final anchored to creation (they differ by only hours).
  (Minor: confirm the tail anchor if it matters.)

### D. Configurable service length + CONSTANTS extraction (user directive)
- Service length must be a configurable value, not hardcoded: add
  **SystemSettings.service_length_days** (default 30) + a field on the Settings page.
  (The Zendesk "Deadline Date" custom field still feeds claim.deadline_date for display/urgency;
  service_length_days drives the UPDATE cadence + final timing. Decide interplay: prefer the
  Zendesk deadline when present, else service_length_days — confirm with user.)
- **Create a constants module** (none exists today; e.g. `apps/communications/constants.py` or
  `apps/core/constants.py`). Move hardcoded durations there:
  - `client_updates.py` `DAY_OFFSETS = [2,5,11,21]` (the early cadence) + the tail step (10) +
    the start (31).
  - `EMAIL_LOOKBACK_DAYS = 2` (apps/communications/services.py).
  - The 30-day default service length.
  - Be on the lookout for any other hardcoded day/duration values while building and move them too.

### E. The autonomous runner (behind an off-by-default flag)
- New flag **SystemSettings.client_updates_autosend** (BooleanField, default False). OFF = today's
  manual behaviour only. ON = autonomous layer runs.
- A **management command** (e.g. `run_client_updates`): when the flag is ON, find DUE updates
  (SCHEDULED, due_at ≤ now, claim still active/searching), re-read the whole ticket, draft, and
  SEND as a public Zendesk reply, then schedule the next milestone. Idempotent + safe to run on
  any cadence. Flag OFF → no-op.
- **Infra (do NOT run apscheduler inside gunicorn — 2 workers = 2 schedulers):** run the command
  from a **Railway scheduled job** (recommend hourly). This is the user's deploy step (like zcli).
  Same runner could later also drive the dormant global email sweep.

### F. Void / stop conditions
- Any important ticket change stops the cascade: client cancels, asks refund, opens a dispute,
  object found, ticket closed/solved. With the cascade, the next link is simply never created;
  the runner also re-checks ticket state before sending a due one.

### G. Object-found = MANUAL (never auto)
- An object-found update is agent-driven: the agent calls the client with the good news and
  sends it themselves. The autonomous runner NEVER auto-sends a "found." (Keep the manual path.)

### H. Exclude harmful institution messages
- Updates must NOT relay harmful/irrelevant institution messages to the client — e.g. an airport
  saying "your submission expired." Add this to the drafting prompt (FOLLOWUP_SYSTEM_PROMPT) and
  the multi-office rule: only relay helpful signals; never bad-for-business notices.

### I. Single source of truth
- All update state lives on the claim in LORA (ClientUpdate + claim fields). Sidebar is a view.
  No duplication.

## Build order
1. Constants module + move DAY_OFFSETS/EMAIL_LOOKBACK_DAYS/service-length default.
2. SystemSettings: service_length_days (default 30) + client_updates_autosend (default False) +
   trigger-by-ID field; Settings page fields (+ optional status-ID picker). Migrations.
3. Cascade refactor: schedule_next(claim) + cadence generator from constants + service_length;
   webhook uses status ID; create only next milestone; tail + final logic.
4. The `run_client_updates` management command (flag-gated, idempotent) + tests.
5. 30→configurable final email (not-found only); object-found stays manual; exclusion rule.
6. Tests (cadence generator for L=30/35/40/45/55; runner flag on/off; void stops; final).
   Remember: tests run against the DEV DB (conftest django_db_setup is a no-op) — run
   `manage.py migrate` before tests see new columns.
7. Docs + commit + push. Railway scheduled job = user's deploy step.

## Confirmed decisions
- Flag off-by-default; gates only the autonomous send (manual hybrid still works).
- 30-day final from claim/ticket CREATION; cadence 2/5/11/21 from SUBMISSION; tail every +10 from 31 < L; final at L.
- Send = PUBLIC Zendesk reply, always agent-approved unless the autosend flag is on.

## Open items to confirm later
- Service length vs Zendesk "Deadline Date": which wins when both exist.
- Tail anchor (submission vs creation) — currently submission.
- Settings status-ID picker (nice-to-have).
