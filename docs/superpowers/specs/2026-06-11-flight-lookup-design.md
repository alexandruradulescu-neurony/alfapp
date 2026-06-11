# Flight Lookup — AeroDataBox Integration (LORA's first action button)

**Date:** 2026-06-11
**Status:** Draft for user review
**Provider decision (user):** AeroDataBox via RapidAPI (free tier ~600 calls/mo;
self-service key). FlightAware AeroAPI noted as the upgrade path; the LORA side
is adapter-shaped so switching later only replaces one module.

## What it does (one paragraph)

An agent clicks **Find flight info** in the Zendesk sidebar. LORA reads the
claim's flight number + date, asks AeroDataBox for the flight, saves the result
on the claim, then runs ONE AI call that cross-checks the real flight against
what the client reported (selected airport, loss time/circumstances) — flagging
mismatches ("client selected OTP but the item was lost in-flight; the aircraft
landed at CDG — search should target CDG / the airline") — writes a history
line, posts flight details **plus the AI validation message** as an internal
note on the ticket, and shows both in the panel. If the flight is NOT found, an
internal note saying so is posted (with the number/date tried). Repeat clicks
reuse the saved result; refresh forces a new lookup. Only a flight number and a
date leave LORA toward the flight API; client-typed text goes only through the
existing protected AI channel.

## Design

### Data
- `SystemSettings.aerodatabox_api_key` — new EncryptedCharField (blank), same
  pattern as the other provider keys. Migration in apps/config.
- `Claim.flight_data` — JSONField (default dict, blank): the normalized lookup
  result. `Claim.flight_data_updated_at` — DateTimeField (null). Migration in
  apps/claims.

### Service module: `apps/integrations/flight_lookup.py`
- `parse_flight_query(flight_details: str) -> {'number','date'} | None` — reads
  the labeled segments our extractor composes
  (`Flight: RO301 | Airline: … | Date/Time: 2026-06-01 …`): flight number from
  the `Flight:` segment (IATA pattern `[A-Z0-9]{2}\s?\d{1,4}`, fallback: scan
  the whole string), date from the `Date/Time:` segment (ISO `YYYY-MM-DD`
  prefix; fallback: any ISO date in the string). Returns None when either is
  missing — the endpoint then answers with a friendly message, never guesses.
- `lookup_flight(number, date) -> dict | None` — GET
  `https://aerodatabox.p.rapidapi.com/flights/number/{number}/{date}` with
  `X-RapidAPI-Key` (+ host header), urllib pattern copied from the sibling
  Zendesk fetchers, 15s timeout. Exact paths/fields verified against the
  AeroDataBox OpenAPI spec at implementation time. None on any failure (logged).
- `normalize_flight(raw) -> dict` — compact shape stored on the claim:
  `{number, airline, status, legs: [{from_iata, from_name, from_city,
  to_iata, to_name, to_city, scheduled_departure_local, scheduled_arrival_local}],
  looked_up_at}`. Raw response is logged at DEBUG, not stored.
- `format_flight_note(data, analysis) -> str` — plaintext internal-note body
  (route, airline, scheduled times, status, "via AeroDataBox"), followed by the
  AI validation block when present ("AI check: …" + bullet mismatches).
- `analyze_flight_match(claim, flight_data) -> FlightCheck | None` — ONE
  AIClient call cross-checking the real flight against the client's report.
  Channels: **trusted** = normalized flight data (provider of our choosing) +
  claim facts; **untrusted** = the client-typed fields (`flight_details` raw
  string incl. the form's Airport segment, `lost_location`,
  `incident_details`) — client text stays in the fenced, PII-tokenized channel.
  `known_pii={'names': [claim.client_name]}`. New schema
  `FlightCheck(summary: str ≤600, mismatches: list[str] ≤5)` in
  apps/ai/schemas.py. The prompt (with ALF_BUSINESS_CONTEXT) asks: do route,
  date and times match the client's selected airport and loss circumstances;
  where should the search focus (departure airport / arrival airport / airline
  for in-flight losses); list concrete mismatches (wrong day, airport not on
  route, flight not operating that date). None on any AI failure — the lookup
  result still saves and posts (analysis is best-effort, like the summary
  engine).

### Endpoint: `POST /api/integrations/zd/flight-lookup/`
Auth: `ZendeskSidebarAuth` + the same failed-attempt rate limiting as the other
sidebar endpoints. Body: `{ticket_id, refresh?: bool}`.

1. Claim by `zd_ticket_id`; none → 200 `{'error_message': 'No LORA claim is
   linked to this ticket.'}` (panel-friendly).
2. `parse_flight_query(claim.flight_details)`; None → 200 `{'error_message':
   "Couldn't read a flight number and date from this claim."}`.
3. Cached (`flight_data` non-empty) and not `refresh` → 200
   `{'flight': …, 'cached': true, 'note_posted': false}`.
4. `lookup_flight(...)`; None → 502 `{'error': 'Flight data provider
   unavailable'}` (transient — no note posted). Empty result list (provider
   answered, flight genuinely not found) → post internal note "Flight
   information was not found for <number> on <date>." + timeline entry, return
   200 `{'error_message': 'No flight found for <number> on <date>.',
   'note_posted': bool}`.
5. Success: save `flight_data` + `flight_data_updated_at`; run
   `analyze_flight_match(claim, flight_data)` (best-effort — None on AI
   failure); ClaimUpdateTimeline entry (`INFO_UPDATED`, changes_summary
   `{'flight_lookup': {number, date}}`, llm_summary = the analysis summary or
   ''); post internal note via existing `post_zendesk_comment` with flight
   details + analysis block (note failure tolerated: `note_posted: false`,
   lookup still succeeds); return `{'flight': …, 'analysis': {summary,
   mismatches} | null, 'cached': false, 'note_posted': bool}`.

Never touches `claim.status` (webhook stays the only stage writer).

### Sidebar app (zendesk_app)
Fifth action button **Find flight info** (plane SVG) in the actions grid →
`loraRequest('/zd/flight-lookup/', {ticket_id})` → renders a compact flight
block under the facts (route with airport codes + names, scheduled times,
status pill) + the standard status line; long-press/secondary "Refresh" not
built — a second click after `cached: true` shows the cached data with a small
"cached — refresh?" link that re-calls with `refresh: true`. Ship via
`zcli apps:update` (user step).

### Tests (inline TDD)
- Parser: labeled string, missing date, missing number, garbage, bare fallback.
- Service: mocked urllib success → normalized shape; HTTP error → None.
- Analysis: mocked AIClient → client text lands in `untrusted`, flight data in
  `trusted`, client name in known_pii; AI failure → None.
- Endpoint (mocked lookup + analysis): no claim; unparseable; cached path skips
  API and AI; success writes flight_data + timeline (llm_summary = analysis
  summary) + note containing the analysis block; flight-not-found posts the
  not-found note; provider failure → 502, no note; analysis failure → lookup
  still succeeds, note posted without AI block; note failure → note_posted
  false but 200.

### Rollout (user steps)
1. Create a RapidAPI account → subscribe to AeroDataBox (free tier).
2. Paste the key into LORA admin → System settings → AeroDataBox API key.
3. `cd zendesk_app && zcli apps:update` (after backend deploy via git push).

## Out of scope
Auto-lookup at claim creation (cost control: on-demand only); FlightAware
adapter; live position tracking; feeding the stored flight data into the
briefing/summary engine prompts (the trust-channel precedent is now set by
`analyze_flight_match` — flight data is TRUSTED; wire it into briefings in a
later increment if agents ask for it).
