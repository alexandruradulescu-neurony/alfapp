# Flight Lookup — AeroDataBox Integration (LORA's first action button)

**Date:** 2026-06-11
**Status:** Draft for user review
**Provider decision (user):** AeroDataBox via RapidAPI (free tier ~600 calls/mo;
self-service key). FlightAware AeroAPI noted as the upgrade path; the LORA side
is adapter-shaped so switching later only replaces one module.

## What it does (one paragraph)

An agent clicks **Find flight info** in the Zendesk sidebar. LORA reads the
claim's flight number + date, asks AeroDataBox for the flight, saves the result
on the claim, writes a history line, posts the details as an **internal note**
on the ticket, and shows the result in the panel. Repeat clicks reuse the saved
result (no extra API spend); a refresh option forces a new lookup. No client
data leaves LORA — only a flight number and a date. No AI involved.

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
- `format_flight_note(data) -> str` — plaintext internal-note body (route,
  airline, scheduled times, status, "via AeroDataBox").

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
   unavailable'}`. Empty result list → 200 `{'error_message': 'No flight found
   for <number> on <date>.'}`.
5. Success: save `flight_data` + `flight_data_updated_at`; ClaimUpdateTimeline
   entry (`INFO_UPDATED`, changes_summary `{'flight_lookup': {number, date}}`,
   llm_summary='' — no AI here); post internal note via existing
   `post_zendesk_comment` (failure tolerated: `note_posted: false` in the
   response, lookup still succeeds); return `{'flight': …, 'cached': false,
   'note_posted': bool}`.

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
- Endpoint (mocked lookup): no claim; unparseable; cached path skips API;
  success writes flight_data + timeline + note (mocked) and returns it; API
  failure → 502; note failure → note_posted false but 200.

### Rollout (user steps)
1. Create a RapidAPI account → subscribe to AeroDataBox (free tier).
2. Paste the key into LORA admin → System settings → AeroDataBox API key.
3. `cd zendesk_app && zcli apps:update` (after backend deploy via git push).

## Out of scope
Auto-lookup at claim creation (cost control: on-demand only); FlightAware
adapter; live position tracking; feeding flight data into AI briefings
(possible later — it would enter the TRUSTED channel since it comes from a
provider we choose, not from ticket text — decide when needed).
