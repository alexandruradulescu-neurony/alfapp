"""Flight lookup via AeroDataBox (RapidAPI) + AI cross-check.

LORA's first action button: the agent clicks in the Zendesk sidebar, LORA
fetches the real flight, cross-checks it against what the client reported
with ONE AIClient call, stores the result on the claim and posts an internal
note on the ticket. When the flight number is not found, the candidate rescue
pulls the stated airport's departures for the day and lets the AI suggest the
likeliest flight instead of dead-ending.

Trust channels: flight data fetched from the provider WE chose = trusted;
client-typed text (flight details, lost location, incident details) = untrusted
(fenced + PII-tokenized). This module never touches claim.status — the Zendesk
webhook stays the only stage writer.
"""
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import time as dt_time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from apps.claims.models import Claim

from apps.ai.client import AIClient
from apps.ai.schemas import FlightCheck
from apps.config.models import SystemSettings
from apps.integrations.briefing import ALF_BUSINESS_CONTEXT
from apps.integrations.services import build_claim_facts

logger = logging.getLogger(__name__)

AERODATABOX_HOST = 'aerodatabox.p.rapidapi.com'
AERODATABOX_TIMEOUT = 15
CANDIDATE_LIMIT = 5
# Pause before the single retry on a 429. NOTE: this is a BLOCKING sleep on the
# synchronous Django request thread (the sidebar button waits on it), and the
# lookup→departures rescue can fire it more than once per click — keep it short.
RATE_LIMIT_RETRY_PAUSE = 1.3

# Sentinel returned by _provider_call when the provider/transport actually failed
# (as opposed to a legitimate empty/no-data answer). Lets callers map an error to
# None while still passing a real empty payload ([] / {}) straight through.
_PROVIDER_ERROR = object()

# Airline designator (RO, W6, 0B, U2) + 1-4 digit flight number, optional space.
_FLIGHT_NUMBER_PATTERN = re.compile(r'\b([A-Z][A-Z0-9]|[0-9][A-Z])\s?(\d{1,4})\b')
_ISO_DATE_PATTERN = re.compile(r'\b(\d{4}-\d{2}-\d{2})\b')
# Zendesk's "Date & Time" form field is free text and usually human English
# ("June 11, 2026 9:15 am") — accept the common shapes, not just ISO.
_MONTH_FIRST_PATTERN = re.compile(
    r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b')
_DAY_FIRST_PATTERN = re.compile(
    r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b')
_SLASH_DATE_PATTERN = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{4})\b')
_MONTH_NUMBERS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
_TIME_HINT_PATTERN = re.compile(r'\b(\d{1,2}):(\d{2})\s*(am|pm)?\b', re.IGNORECASE)
_PAREN_IATA_PATTERN = re.compile(r'\(([A-Za-z]{3})\)')
_IATA_TOKEN_PATTERN = re.compile(r'\b[A-Z]{3}\b')
# 3-letter words that show up in airport names and are not IATA hints here
# (LOS/SAN are real IATA codes, but as words they are far likelier city-name
# fragments like "Los Angeles" / "San Francisco" than intended codes).
_IATA_STOPWORDS = {'NEW', 'THE', 'AND', 'FOR', 'INT', 'AIR', 'DEL', 'VON', 'LOS', 'SAN'}

FLIGHT_CHECK_PROMPT = ALF_BUSINESS_CONTEXT + (
    "You are validating flight data for a lost-item case. Compare the verified "
    "flight data (trusted) with what the client reported (untrusted): does the "
    "route include the airport the client selected, and do the date and times "
    "fit when and where they say the item was lost? Say clearly where the "
    "search should focus: the departure airport, the arrival airport, or the "
    "airline for items lost on board the aircraft. If candidate flights are "
    "provided instead of a verified flight, say which candidate best fits the "
    "client's story and why. If the flight has multiple legs (e.g. an "
    "out-and-back rotation under one number), say which leg the client most "
    "likely took, using their reported time and airport. List concrete "
    "mismatches (wrong day, airport not on the route, flight not operating "
    "that date). Report a mismatch ONLY when the provided content genuinely "
    "contradicts itself — an empty mismatches list means everything fits. "
    "Keep the summary to 2-4 short sentences, under 500 characters. "
    "Use ONLY the provided content; never invent flights, airports or times. "
    'Respond as JSON: {"summary": "...", "mismatches": ["..."]}.'
)


class FlightProviderNotConfigured(Exception):
    """Raised when the AeroDataBox API key is missing from SystemSettings."""


def _segment(text: str, *labels: str) -> str:
    """Extract one labeled segment from labeled text. Handles both the
    composed claim string ('Flight: RO301 | Airline: TAROM | …', pipe-joined)
    and raw ticket descriptions (one 'Label: value' per line). Accepts label
    aliases ('Flight', 'Flight #')."""
    parts = re.split(r'[|\n]', text or '')
    for part in parts:
        part = part.strip()
        for label in labels:
            prefix = label.lower() + ':'
            if part.lower().startswith(prefix):
                return part[len(prefix):].strip()
    return ''


def _parse_date_text(text: str) -> Optional[str]:
    """ISO ('2026-06-11'), human English ('June 11, 2026' / '11 June 2026'),
    or slash dates ('06/11/2026', US month-first; flipped when the first
    number cannot be a month) -> 'YYYY-MM-DD' | None."""
    match = _ISO_DATE_PATTERN.search(text)
    if match:
        return match.group(1)
    match = _MONTH_FIRST_PATTERN.search(text)
    if match:
        month = _MONTH_NUMBERS.get(match.group(1)[:3].lower())
        day, year = int(match.group(2)), int(match.group(3))
        if month and 1 <= day <= 31:
            return f'{year:04d}-{month:02d}-{day:02d}'
    match = _DAY_FIRST_PATTERN.search(text)
    if match:
        month = _MONTH_NUMBERS.get(match.group(2)[:3].lower())
        day, year = int(match.group(1)), int(match.group(3))
        if month and 1 <= day <= 31:
            return f'{year:04d}-{month:02d}-{day:02d}'
    match = _SLASH_DATE_PATTERN.search(text)
    if match:
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        month, day = (first, second) if first <= 12 else (second, first)
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f'{year:04d}-{month:02d}-{day:02d}'
    return None


def _airline_code(airline_segment: str) -> Optional[str]:
    """IATA airline designator from the form's Airline field
    ('American Airlines - AA' -> 'AA', 'Wizz Air W6' -> 'W6'): the last
    standalone two-character token."""
    matches = re.findall(r'\b([A-Z][A-Z0-9])\b', (airline_segment or '').upper())
    return matches[-1] if matches else None


def parse_flight_query(flight_details: str) -> Optional[Dict[str, str]]:
    """{'number': 'RO301', 'date': '2026-06-01'} from the claim's flight
    details, or None when either piece is missing. Prefers the labeled
    segments our extractor composes; falls back to scanning the whole string.
    A bare-digits Flight field ('377') borrows the airline code from the
    Airline segment ('American Airlines - AA' -> AA377) — clients often type
    just the number."""
    text = flight_details or ''
    flight_seg = _segment(text, 'Flight')
    number = None
    number_match = _FLIGHT_NUMBER_PATTERN.search((flight_seg or text).upper())
    if number_match:
        number = (number_match.group(1) + number_match.group(2)).replace(' ', '')
    elif flight_seg:
        bare = re.fullmatch(r'(\d{1,4})', flight_seg.strip())
        code = _airline_code(_segment(text, 'Airline')) if bare else None
        if bare and code:
            number = code + bare.group(1)

    date = parse_date_hint(text)

    if not number or not date:
        return None
    return {'number': number, 'date': date}


def parse_date_hint(flight_details: str) -> Optional[str]:
    """'YYYY-MM-DD' from the 'Date/Time:' segment, falling back to anywhere
    in the string. None when no recognizable date."""
    text = flight_details or ''
    date_source = _segment(text, 'Date/Time') or text
    return _parse_date_text(date_source) or _parse_date_text(text)


def parse_airline_hint(flight_details: str) -> Optional[str]:
    """IATA carrier code from the 'Airline:' segment ('American Airlines - AA'
    -> 'AA'), or None."""
    return _airline_code(_segment(flight_details or '', 'Airline'))


def parse_airport_hint(flight_details: str) -> Optional[str]:
    """IATA code of the airport the client selected on the form, when one is
    recognizable in the 'Airport:' segment. None otherwise (no guessing)."""
    seg = _segment(flight_details or '', 'Airport')
    if not seg:
        return None
    paren = _PAREN_IATA_PATTERN.search(seg)
    if paren:
        return paren.group(1).upper()
    tokens = [t for t in _IATA_TOKEN_PATTERN.findall(seg.upper())
              if t not in _IATA_STOPWORDS]
    return tokens[0] if tokens else None


def parse_time_hint(flight_details: str) -> Optional[dt_time]:
    """HH:MM from the 'Date/Time:' segment, or None."""
    seg = _segment(flight_details or '', 'Date/Time')
    # ISO 'T' separators would make the first HH:MM match land on minutes
    # (…T14:20:00 -> '20:00'); normalize to a space first.
    seg = seg.replace('T', ' ')
    match = _TIME_HINT_PATTERN.search(seg)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = (match.group(3) or '').lower()
    if meridiem == 'pm' and hour != 12:
        hour += 12
    elif meridiem == 'am' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return dt_time(hour, minute)


def _aerodatabox_get(path: str) -> Any:
    """GET against AeroDataBox with the SystemSettings key. Raises
    FlightProviderNotConfigured when the key is missing; lets urllib errors
    propagate (callers map them to their own semantics). Retries ONCE after a
    short pause on HTTP 429 — the Basic plan rate-limits per SECOND (verified
    live), and our lookup→departures rescue fires two calls back to back."""
    api_key = SystemSettings.get_instance().aerodatabox_api_key
    if not api_key:
        raise FlightProviderNotConfigured('AeroDataBox API key not set in SystemSettings')
    req = urllib.request.Request(
        f'https://{AERODATABOX_HOST}{path}',
        headers={
            'X-RapidAPI-Key': api_key,
            'X-RapidAPI-Host': AERODATABOX_HOST,
            # RapidAPI's edge rejects Python's default urllib User-Agent with
            # 403 (verified live: same key, curl 200 vs urllib 403).
            'User-Agent': 'LORA-flight-lookup/1.0',
        },
        method='GET',
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=AERODATABOX_TIMEOUT) as response:
                body = response.read()
                # AeroDataBox signals "no data" with HTTP 204 + empty body (NOT 404).
                if response.status == 204 or not body:
                    return None
                return json.loads(body.decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                logger.info("AeroDataBox per-second rate limit hit; retrying once")
                time.sleep(RATE_LIMIT_RETRY_PAUSE)  # blocks the request thread (see constant)
                continue
            raise


def _provider_call(fetch, empty, *, label: str):
    """Run a provider GET (`fetch`, a zero-arg callable) applying the module's one
    error policy so both callers translate failures identically:
    FlightProviderNotConfigured propagates; HTTP 404 → the caller's `empty`
    sentinel; any other transport/provider error → _PROVIDER_ERROR (logged with
    `label`). On success returns whatever `fetch` returned."""
    try:
        return fetch()
    except FlightProviderNotConfigured:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return empty
        logger.error(f"AeroDataBox {label} HTTP {e.code}")
        return _PROVIDER_ERROR
    except Exception as e:
        logger.error(f"AeroDataBox {label} failed: {e}")
        return _PROVIDER_ERROR


def lookup_flight(number: str, date: str) -> Optional[List[Dict[str, Any]]]:
    """Flight legs for a flight number on a local date.
    Returns a list of raw leg dicts; [] when the provider answered but found
    nothing (HTTP 204 empty body, or 404); None on transport/provider errors."""
    result = _provider_call(
        lambda: _aerodatabox_get(f'/flights/number/{number}/{date}'),
        empty=[], label=f"flight lookup {number} {date}")
    if result is _PROVIDER_ERROR:
        return None
    return result if isinstance(result, list) else []


def _fetch_departures_window(airport_iata: str, date: str,
                             from_hour: int, to_hour: int) -> Optional[Dict[str, Any]]:
    """Departures FIDS for one sub-12h window. Returns the provider dict on
    success, {} on 204/404 'no data', and None on transport/provider error."""
    path = (f'/flights/airports/iata/{airport_iata}'
            f'/{date}T{from_hour:02d}:00/{date}T{to_hour:02d}:59'
            f'?direction=Departure&withCancelled=true&withCodeshared=false&withLeg=false')
    result = _provider_call(
        lambda: _aerodatabox_get(path) or {},
        empty={}, label=f"departures {airport_iata} {date}")
    return None if result is _PROVIDER_ERROR else result


def _candidate_minute(candidate: Dict[str, str]) -> Optional[int]:
    """Minute-of-day parsed from a candidate's scheduled_local string (the first
    HH:MM in it), or None when it has no parseable time."""
    m = re.search(r'(\d{1,2}):(\d{2})', candidate.get('scheduled_local', '') or '')
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _rank_candidates(candidates: List[Dict[str, str]],
                     time_hint: Optional[dt_time]) -> List[Dict[str, str]]:
    """Order candidates so the CANDIDATE_LIMIT cap keeps the most relevant ones.
    With a time hint: closest to it first. Without one: chronological. Candidates
    with no parseable time sort last. Stable, so same-key order is preserved."""
    if time_hint is not None:
        target = time_hint.hour * 60 + time_hint.minute
        return sorted(candidates, key=lambda c: (
            _candidate_minute(c) is None, abs((_candidate_minute(c) or 0) - target)))
    return sorted(candidates, key=lambda c: (
        _candidate_minute(c) is None, _candidate_minute(c) or 0))


def find_candidate_flights(airport_iata: str, date: str,
                           time_hint: Optional[dt_time] = None,
                           destination_hint: str = '',
                           airline_code: str = '') -> Optional[List[Dict[str, str]]]:
    """Departures from the stated airport around the stated time — the rescue
    path when the flight number is wrong or missing. Window: time hint ±3h in
    one call; no hint -> two calls covering the whole day (AeroDataBox FIDS
    windows must stay UNDER 12h each). `airline_code` ('AA') filters to that
    carrier; `destination_hint` filters toward a destination when one is ever
    available (future form field).
    Returns compact candidates capped at CANDIDATE_LIMIT; [] when none;
    None when every window errored."""
    if time_hint:
        windows = [(max(time_hint.hour - 3, 0), min(time_hint.hour + 3, 23))]
    else:
        windows = [(0, 11), (12, 23)]

    departures = []
    any_ok = False
    for from_hour, to_hour in windows:
        result = _fetch_departures_window(airport_iata, date, from_hour, to_hour)
        if result is None:
            continue
        any_ok = True
        departures.extend(result.get('departures') or [])
    if not any_ok:
        return None

    candidates = []
    wanted = (destination_hint or '').strip().upper()
    carrier = (airline_code or '').strip().upper()
    for dep in departures:
        number = (dep.get('number') or '')
        if carrier and not number.replace(' ', '').upper().startswith(carrier):
            continue
        movement = dep.get('movement') or {}
        airport = movement.get('airport') or {}
        destination = ' '.join(p for p in [airport.get('iata', ''), airport.get('name', '')] if p)
        if wanted and wanted not in destination.upper():
            continue
        candidates.append({
            'number': number,
            'destination': destination,
            'scheduled_local': (movement.get('scheduledTime') or {}).get('local', ''),
        })

    # Rank BEFORE truncating: without a sort the morning window (00:00–11:59)
    # always filled the cap and afternoon/evening flights were silently dropped —
    # possibly the one matching the client's loss time. With a time hint we keep
    # the closest flights; without one, the earliest (deterministic).
    ranked = _rank_candidates(candidates, time_hint)
    if len(ranked) > CANDIDATE_LIMIT:
        logger.info("Flight candidates for %s %s: %d found, keeping the %d most relevant",
                    airport_iata, date, len(ranked), CANDIDATE_LIMIT)
    return ranked[:CANDIDATE_LIMIT]


def normalize_flight(raw_legs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact, stable shape stored on the claim (raw response stays out of
    the DB; it is logged at DEBUG by the caller if needed)."""
    first = raw_legs[0] if raw_legs else {}
    legs = []
    for leg in raw_legs:
        departure = leg.get('departure') or {}
        arrival = leg.get('arrival') or {}
        dep_airport = departure.get('airport') or {}
        arr_airport = arrival.get('airport') or {}
        legs.append({
            'from_iata': dep_airport.get('iata', ''),
            'from_name': dep_airport.get('name', ''),
            'from_city': dep_airport.get('municipalityName', ''),
            'to_iata': arr_airport.get('iata', ''),
            'to_name': arr_airport.get('name', ''),
            'to_city': arr_airport.get('municipalityName', ''),
            'scheduled_departure_local': (departure.get('scheduledTime') or {}).get('local', ''),
            'scheduled_arrival_local': (arrival.get('scheduledTime') or {}).get('local', ''),
            'status': leg.get('status', ''),
            'from_terminal': departure.get('terminal', '') or '',
            'from_gate': departure.get('gate', '') or '',
            'to_terminal': arrival.get('terminal', '') or '',
            'to_gate': arrival.get('gate', '') or '',
            'to_baggage_belt': arrival.get('baggageBelt', '') or '',
        })
    return {
        'number': first.get('number', ''),
        'airline': (first.get('airline') or {}).get('name', ''),
        'status': first.get('status', ''),
        'legs': legs,
        'looked_up_at': timezone.now().isoformat(),
    }


def analyze_flight_match(claim: 'Optional[Claim]',
                         flight_payload: Optional[Dict[str, Any]] = None,
                         candidates: Optional[List[Dict[str, str]]] = None,
                         flight_details_text: str = '') -> Optional[FlightCheck]:
    """ONE AIClient call cross-checking flight reality vs the client's report.
    Returns a FlightCheck or None on any AI failure — callers must treat the
    analysis as optional (the lookup result stands on its own).

    `claim` may be None (claimless tickets): pass the flight details composed
    from the ticket's Zendesk fields as `flight_details_text` instead — it is
    client-form data and stays in the untrusted channel."""
    trusted = {}
    if claim is not None:
        trusted['claim_facts'] = str(build_claim_facts(claim))
    if flight_payload:
        trusted['verified_flight_data'] = json.dumps(flight_payload, ensure_ascii=False)
    if candidates:
        trusted['candidate_flights'] = json.dumps(candidates, ensure_ascii=False)

    untrusted = {}
    details = claim.flight_details if claim is not None else flight_details_text
    if details:
        untrusted['client_reported_flight'] = details
    if claim is not None and claim.lost_location:
        untrusted['client_lost_location'] = claim.lost_location
    if claim is not None and claim.incident_details:
        untrusted['client_incident_details'] = claim.incident_details

    names = [claim.client_name] if claim is not None and claim.client_name else []
    known_pii = {'aliases': [], 'names': names}
    subject = f"claim #{claim.id}" if claim is not None else "claimless ticket"
    try:
        return AIClient.complete(
            system_prompt=FLIGHT_CHECK_PROMPT,
            trusted=trusted or None,
            untrusted=untrusted or None,
            known_pii=known_pii,
            response_schema=FlightCheck,
            call_site='flight_check',
            temperature=0.3,
            max_tokens=400,
        )
    except Exception as e:
        logger.warning(f"Flight cross-check failed for {subject}: {e}")
        return None


VERDICT_EMOJI = {
    'verified': '✅',
    'check': '⚠️',
    'unchecked': 'ℹ️',
    'not_found': '❌',
}


def derive_flight_verdict(found: bool, analysis,
                          has_candidates: bool = False) -> Dict[str, str]:
    """Rule-derived, agent-readable verdict — never an invented number.
    The level is computed from facts (flight found? cross-check ran? did it
    flag mismatches?); the AI only contributes the mismatch list it already
    produces, so the label cannot hallucinate optimism."""
    if not found:
        label = 'Flight NOT found'
        if has_candidates:
            label += ' — likely candidates listed below'
        return {'level': 'not_found', 'label': label}
    if analysis is None:
        return {'level': 'unchecked',
                'label': 'Flight found — AI cross-check unavailable, verify manually'}
    if analysis.mismatches:
        return {'level': 'check',
                'label': 'Flight found — verify details before acting'}
    return {'level': 'verified',
            'label': "Flight verified — consistent with the client's report"}


def _verdict_line(verdict) -> str:
    if not verdict:
        return ''
    return f"{VERDICT_EMOJI.get(verdict['level'], '')} {verdict['label']}".strip()


def _airport_label(iata: str, name: str, city: str) -> str:
    """'DTW (Detroit)' — the city is the human anchor; airport name only when
    the city is missing. Avoids 'Tampa, Tampa' duplication."""
    place = city or name
    if iata and place:
        return f'{iata} ({place})'
    return iata or place or '?'


def _clock(scheduled_local: str) -> str:
    """'2026-06-11 07:00-04:00' -> '07:00'."""
    match = re.search(r'\b(\d{2}:\d{2})', scheduled_local or '')
    return match.group(1) if match else ''


def _leg_facilities(leg: Dict[str, Any]) -> str:
    """'dep Terminal B, Gate 22 · arr Terminal 2E, Belt 7' — only the pieces
    the provider actually has; '' when none (gates/belts appear close to the
    flight and fade from history)."""
    dep_bits = [bit for bit in [
        f"Terminal {leg.get('from_terminal')}" if leg.get('from_terminal') else '',
        f"Gate {leg.get('from_gate')}" if leg.get('from_gate') else '',
    ] if bit]
    arr_bits = [bit for bit in [
        f"Terminal {leg.get('to_terminal')}" if leg.get('to_terminal') else '',
        f"Gate {leg.get('to_gate')}" if leg.get('to_gate') else '',
        f"Belt {leg.get('to_baggage_belt')}" if leg.get('to_baggage_belt') else '',
    ] if bit]
    parts = []
    if dep_bits:
        parts.append('dep ' + ', '.join(dep_bits))
    if arr_bits:
        parts.append('arr ' + ', '.join(arr_bits))
    return ' · '.join(parts)


def _analysis_block(analysis) -> str:
    if not analysis:
        return ''
    lines = ['', f"AI check: {analysis.summary}"]
    if analysis.mismatches:
        lines.append('Mismatches:')
        lines.extend(f'• {m}' for m in analysis.mismatches)
    return '\n'.join(lines)


def format_flight_note(flight: Dict[str, Any], analysis, verdict=None) -> str:
    """Internal-note body for a found flight: verdict first, one compact line
    per leg, then the AI check. Plain text — renders everywhere in Zendesk."""
    legs = flight.get('legs', [])
    date = ''
    if legs:
        date = (legs[0].get('scheduled_departure_local') or '')[:10]

    lines = []
    if verdict:
        lines.append(_verdict_line(verdict))
        lines.append('')
    header = ' — '.join(p for p in [
        f"Flight {flight.get('number', '')}".strip(),
        flight.get('airline', ''),
        date,
    ] if p)
    lines.append(header)

    label_legs = len(legs) > 1
    for i, leg in enumerate(legs, 1):
        route = (f"{_airport_label(leg['from_iata'], leg['from_name'], leg['from_city'])} "
                 f"{_clock(leg['scheduled_departure_local'])} → "
                 f"{_airport_label(leg['to_iata'], leg['to_name'], leg['to_city'])} "
                 f"{_clock(leg['scheduled_arrival_local'])}").replace('  ', ' ').strip()
        status = leg.get('status', '') or (flight.get('status', '') if not label_legs else '')
        line = f"Leg {i}: {route}" if label_legs else f"Route: {route}"
        if status:
            line += f" — {status}"
        lines.append(line)
        facilities = _leg_facilities(leg)
        if facilities:
            lines.append(facilities)
    lines.append('')
    lines.append('(times are local; via AeroDataBox)')
    return '\n'.join(lines) + _analysis_block(analysis)


def _candidate_lines(candidates: List[Dict[str, str]]) -> List[str]:
    lines = []
    for c in candidates:
        entry = ' '.join(p for p in [
            c.get('number', ''),
            f"→ {c['destination']}" if c.get('destination') else '',
            f"dep {_clock(c.get('scheduled_local')) or c.get('scheduled_local', '')}"
            if c.get('scheduled_local') else '',
        ] if p)
        lines.append(f'• {entry}')
    return lines


def format_candidates_note(number: str, date: str, airport_iata: str,
                           candidates: List[Dict[str, str]], analysis,
                           verdict=None) -> str:
    """Internal-note body for the not-found-with-candidates rescue."""
    lines = []
    if verdict:
        lines.append(_verdict_line(verdict))
        lines.append('')
    lines.append(f"Flight {number} not found on {date} — "
                 f"likely candidates departing {airport_iata}:")
    lines.extend(_candidate_lines(candidates))
    lines.append('')
    lines.append('(times are local; via AeroDataBox)')
    return '\n'.join(lines) + _analysis_block(analysis)


def format_no_number_note(date: str, airport_iata: str,
                          candidates: List[Dict[str, str]], analysis,
                          verdict=None, airline_code: str = '') -> str:
    """Internal-note body for the no-flight-number search (airport + date,
    optionally narrowed to one carrier)."""
    lines = []
    if verdict:
        lines.append(_verdict_line(verdict))
        lines.append('')
    headline = (f"No flight number on this ticket — likely candidates "
                f"departing {airport_iata} on {date}")
    if airline_code:
        headline += f" ({airline_code} flights only)"
    lines.append(headline + ':')
    lines.extend(_candidate_lines(candidates))
    lines.append('')
    lines.append('(times are local; via AeroDataBox)')
    return '\n'.join(lines) + _analysis_block(analysis)


def format_not_found_note(number: str, date: str, verdict=None) -> str:
    prefix = _verdict_line(verdict)
    body = f"Flight information was not found for {number} on {date}."
    return f'{prefix}\n\n{body}' if prefix else body
