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
import urllib.error
import urllib.request
from datetime import time as dt_time
from typing import Any, Dict, List, Optional

from django.utils import timezone

from apps.ai.client import AIClient
from apps.ai.schemas import FlightCheck
from apps.config.models import SystemSettings
from apps.integrations.briefing import ALF_BUSINESS_CONTEXT
from apps.integrations.services import build_claim_facts

logger = logging.getLogger(__name__)

AERODATABOX_HOST = 'aerodatabox.p.rapidapi.com'
AERODATABOX_TIMEOUT = 15
CANDIDATE_LIMIT = 5

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
    "client's story and why. List concrete mismatches (wrong day, airport not "
    "on the route, flight not operating that date). Use ONLY the provided "
    "content; never invent flights, airports or times. "
    'Respond as JSON: {"summary": "...", "mismatches": ["..."]}.'
)


class FlightProviderNotConfigured(Exception):
    """Raised when the AeroDataBox API key is missing from SystemSettings."""


def _segment(flight_details: str, label: str) -> str:
    """Extract one labeled segment from the composed flight_details string
    ('Flight: RO301 | Airline: TAROM | Date/Time: 2026-06-01 14:20')."""
    for part in (flight_details or '').split('|'):
        part = part.strip()
        if part.lower().startswith(label.lower() + ':'):
            return part[len(label) + 1:].strip()
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


def parse_flight_query(flight_details: str) -> Optional[Dict[str, str]]:
    """{'number': 'RO301', 'date': '2026-06-01'} from the claim's flight
    details, or None when either piece is missing. Prefers the labeled
    segments our extractor composes; falls back to scanning the whole string."""
    text = flight_details or ''
    number_source = _segment(text, 'Flight') or text
    number_match = _FLIGHT_NUMBER_PATTERN.search(number_source.upper())

    date_source = _segment(text, 'Date/Time') or text
    date = _parse_date_text(date_source) or _parse_date_text(text)

    if not number_match or not date:
        return None
    number = (number_match.group(1) + number_match.group(2)).replace(' ', '')
    return {'number': number, 'date': date}


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
    propagate (callers map them to their own semantics)."""
    api_key = SystemSettings.get_instance().aerodatabox_api_key
    if not api_key:
        raise FlightProviderNotConfigured('AeroDataBox API key not set in SystemSettings')
    req = urllib.request.Request(
        f'https://{AERODATABOX_HOST}{path}',
        headers={
            'X-RapidAPI-Key': api_key,
            'X-RapidAPI-Host': AERODATABOX_HOST,
        },
        method='GET',
    )
    with urllib.request.urlopen(req, timeout=AERODATABOX_TIMEOUT) as response:
        body = response.read()
        # AeroDataBox signals "no data" with HTTP 204 + empty body (NOT 404).
        if response.status == 204 or not body:
            return None
        return json.loads(body.decode('utf-8'))


def lookup_flight(number: str, date: str) -> Optional[List[Dict[str, Any]]]:
    """Flight legs for a flight number on a local date.
    Returns a list of raw leg dicts; [] when the provider answered but found
    nothing (HTTP 204 empty body, or 404); None on transport/provider errors."""
    try:
        result = _aerodatabox_get(f'/flights/number/{number}/{date}')
    except FlightProviderNotConfigured:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        logger.error(f"AeroDataBox flight lookup HTTP {e.code} for {number} {date}")
        return None
    except Exception as e:
        logger.error(f"AeroDataBox flight lookup failed for {number} {date}: {e}")
        return None
    if isinstance(result, list):
        return result
    return []


def find_candidate_flights(airport_iata: str, date: str,
                           time_hint: Optional[dt_time] = None,
                           destination_hint: str = '') -> Optional[List[Dict[str, str]]]:
    """Departures from the stated airport around the stated time — the rescue
    path when the flight number is not found. Window: time hint ±3h, else
    08:00-19:59 (AeroDataBox FIDS windows must stay UNDER 12h per call).
    `destination_hint` is accepted for future wiring; the current caller has
    no reliable destination source and passes nothing.
    Returns compact candidates capped at CANDIDATE_LIMIT; [] when none;
    None on transport/provider errors."""
    if time_hint:
        from_hour = max(time_hint.hour - 3, 0)
        to_hour = min(time_hint.hour + 3, 23)
    else:
        from_hour, to_hour = 8, 19
    path = (f'/flights/airports/iata/{airport_iata}'
            f'/{date}T{from_hour:02d}:00/{date}T{to_hour:02d}:59'
            f'?direction=Departure&withCancelled=true&withCodeshared=false&withLeg=false')
    try:
        result = _aerodatabox_get(path)
    except FlightProviderNotConfigured:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        logger.error(f"AeroDataBox departures HTTP {e.code} for {airport_iata} {date}")
        return None
    except Exception as e:
        logger.error(f"AeroDataBox departures failed for {airport_iata} {date}: {e}")
        return None

    if not result:
        return []
    candidates = []
    wanted = (destination_hint or '').strip().upper()
    for dep in (result.get('departures') or []):
        movement = dep.get('movement') or {}
        airport = movement.get('airport') or {}
        destination = ' '.join(p for p in [airport.get('iata', ''), airport.get('name', '')] if p)
        if wanted and wanted not in destination.upper():
            continue
        candidates.append({
            'number': dep.get('number', ''),
            'destination': destination,
            'scheduled_local': (movement.get('scheduledTime') or {}).get('local', ''),
        })
        if len(candidates) >= CANDIDATE_LIMIT:
            break
    return candidates


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
        })
    return {
        'number': first.get('number', ''),
        'airline': (first.get('airline') or {}).get('name', ''),
        'status': first.get('status', ''),
        'legs': legs,
        'looked_up_at': timezone.now().isoformat(),
    }


def analyze_flight_match(claim, flight_payload: Optional[Dict[str, Any]] = None,
                         candidates: Optional[List[Dict[str, str]]] = None):
    """ONE AIClient call cross-checking flight reality vs the client's report.
    Returns a FlightCheck or None on any AI failure — callers must treat the
    analysis as optional (the lookup result stands on its own)."""
    trusted = {'claim_facts': str(build_claim_facts(claim))}
    if flight_payload:
        trusted['verified_flight_data'] = json.dumps(flight_payload, ensure_ascii=False)
    if candidates:
        trusted['candidate_flights'] = json.dumps(candidates, ensure_ascii=False)

    untrusted = {}
    if claim.flight_details:
        untrusted['client_reported_flight'] = claim.flight_details
    if claim.lost_location:
        untrusted['client_lost_location'] = claim.lost_location
    if claim.incident_details:
        untrusted['client_incident_details'] = claim.incident_details

    known_pii = {'aliases': [], 'names': [n for n in [claim.client_name] if n]}
    try:
        return AIClient.complete(
            system_prompt=FLIGHT_CHECK_PROMPT,
            trusted=trusted,
            untrusted=untrusted or None,
            known_pii=known_pii,
            response_schema=FlightCheck,
            call_site='flight_check',
            temperature=0.3,
            max_tokens=400,
        )
    except Exception as e:
        logger.warning(f"Flight cross-check failed for claim #{claim.id}: {e}")
        return None


def _analysis_block(analysis) -> str:
    if not analysis:
        return ''
    lines = [f"\nAI check: {analysis.summary}"]
    if analysis.mismatches:
        lines.append('Mismatches:')
        lines.extend(f'- {m}' for m in analysis.mismatches)
    return '\n'.join(lines)


def format_flight_note(flight: Dict[str, Any], analysis) -> str:
    """Internal-note body for a found flight (+ optional AI check block)."""
    lines = ['Flight lookup (AeroDataBox)']
    header = ' — '.join(p for p in [
        f"Flight {flight.get('number', '')}".strip(),
        flight.get('airline', ''),
        f"status: {flight.get('status', '')}" if flight.get('status') else '',
    ] if p)
    lines.append(header)
    for leg in flight.get('legs', []):
        route = (f"{leg['from_iata']} ({leg['from_name']}, {leg['from_city']}) -> "
                 f"{leg['to_iata']} ({leg['to_name']}, {leg['to_city']})")
        lines.append(route)
        times = ' | '.join(p for p in [
            f"dep {leg['scheduled_departure_local']}" if leg['scheduled_departure_local'] else '',
            f"arr {leg['scheduled_arrival_local']}" if leg['scheduled_arrival_local'] else '',
        ] if p)
        if times:
            lines.append(f'Scheduled: {times}')
    return '\n'.join(lines) + _analysis_block(analysis)


def format_candidates_note(number: str, date: str, airport_iata: str,
                           candidates: List[Dict[str, str]], analysis) -> str:
    """Internal-note body for the not-found-with-candidates rescue."""
    lines = [
        f"Flight {number} not found on {date} — likely candidates departing {airport_iata}:",
    ]
    for c in candidates:
        entry = ' '.join(p for p in [
            c.get('number', ''),
            f"-> {c['destination']}" if c.get('destination') else '',
            f"dep {c['scheduled_local']}" if c.get('scheduled_local') else '',
        ] if p)
        lines.append(f'- {entry}')
    return '\n'.join(lines) + _analysis_block(analysis)


def format_not_found_note(number: str, date: str) -> str:
    return f"Flight information was not found for {number} on {date}."
