# Browser Use Cloud API — confirmed shapes (probed 2026-06-22)

Confirmed live against the account key (two tiny `example.com` probe runs, ~$0.04 each).
These are the facts the `apps/integrations/browser_use.py` wrapper is built on.

## Auth & base
- Base URL: `https://api.browser-use.com/api/v3`
- Header: `X-Browser-Use-API-Key: bu_…`

## Casing (important)
- **Request bodies are snake_case**: `task`, `secrets`, `allowed_domains`, `enable_recording`, `keep_alive`, `session_id`, `model`, `output_schema`, `proxy_country_code`.
- **Responses are camelCase**: `id`, `liveUrl`, `screenshotUrl`, `status`, `output`, `isTaskSuccessful`, `workspaceId`, `recordingUrls`, `stepCount`, `createdAt`. The wrapper normalizes these to snake_case internally (`live_url`, `screenshot_url`).

## Create a session
- `POST /api/v3/sessions` → **201**. Body: `{task, secrets, allowed_domains, enable_recording, keep_alive, model?}`.
- Response includes `id` (the session id), `liveUrl` (interactive live view), `status` (`running`), `screenshotUrl` (null until steps produce one), `workspaceId`, `recordingUrls`.
- **Nested secrets accepted**: `secrets: {"<host>": {"x_name": "value", …}}` → 201.
- `model`: sent `claude-sonnet-4.6`, response echoed `bu-max` — model selection is effectively overridden to `bu-max` (their top tier) on this account. Harmless; runs fine. We still send the configured `model` (ignored if unsupported).

## keep_alive (the approve→submit mechanism)
- With `keep_alive: true` on create, the session goes to **`idle`** after the task completes (NOT `stopped`), so it accepts follow-ups. CONFIRMED.
- Without `keep_alive`, the session goes to **`stopped`** after its task, and a stopped session **rejects** new tasks: `400 {"detail":"Session must be idle or created to accept tasks (current: stopped)"}`.
- **keep_alive applies per task.** A follow-up task that omits `keep_alive` runs and then lets the session stop. So: fill call uses `keep_alive: true`; the submit follow-up omits it (final action) and the session stops naturally afterward.

## Status
- `GET /api/v3/sessions/{id}` → `status` ∈ `running | idle | stopped | error | timed_out` (+ `output`, `screenshotUrl`, `isTaskSuccessful`).
- A `stopped`/`idle` session still returns full final state on GET (so we read `output` + `screenshotUrl` after submit even though it stopped).
- Inactivity timeout ~15 min; max ~4 h.

## Follow-up task (submit step)
- `POST /api/v3/sessions` with `{task, session_id}` → **200**, only when the session is `idle` or `created`. Browser state (page, cookies) carries over — the follow-up correctly referenced the prior step's result.

## Screenshots
- Session-level `screenshotUrl` (camelCase) is the simplest source (newest shot). Also per-message: `GET /api/v3/sessions/{id}/messages` → `{messages:[{… screenshotUrl …}], hasMore}`.
- `null` for trivial/0-step tasks; real multi-step form fills will populate it. Fallback for the agent = the live view.

## Stop / cleanup
- `POST /api/v3/sessions/{id}/stop` with `{strategy: "session"}` (destroy) or `{strategy: "task"}` (stop current task only) → 200.

## Files (for Task 7 image upload)
- `GET /api/v3/workspaces` → 200, returns workspaces (a default "My Files" exists). `GET /api/v3/sessions/{id}/files` → 200 (`{files, folders, …}`). Exact upload mechanics (workspace upload vs presigned) to be pinned when implementing Task 7.

## Cost
- ~$0.039 for a trivial run on `bu-max`. Real form fills cost more (more tokens/steps) but remain small.
