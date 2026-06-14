"""Claim-domain pure helpers (no model imports — safe for migrations)."""
import re
from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Evidence-upload limits — shared so the API and the frontend agree on what an
# acceptable image is (the API path previously did no validation at all).
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
EVIDENCE_ALLOWED_EXTENSIONS = ('jpg', 'jpeg', 'png', 'gif', 'webp')


def validate_evidence_image(image) -> None:
    """Size + extension validation for an uploaded evidence image. Raises
    django.core.exceptions.ValidationError on failure. Pure (no model imports)."""
    from django.core.exceptions import ValidationError
    if image is None:
        raise ValidationError('An image file is required.')
    if image.size > EVIDENCE_MAX_BYTES:
        raise ValidationError(f'File must be under {EVIDENCE_MAX_BYTES // 1024 // 1024}MB.')
    ext = image.name.rsplit('.', 1)[-1].lower() if '.' in (image.name or '') else ''
    if ext not in EVIDENCE_ALLOWED_EXTENSIONS:
        raise ValidationError(
            f'Invalid file type. Allowed: {", ".join(EVIDENCE_ALLOWED_EXTENSIONS)}.')

# Common human-typed abbreviations -> IANA zone. Fallback is UTC; precision
# beyond "right day" is best-effort by design (see spec §6).
TZ_ABBREVIATIONS = {
    'UTC': 'UTC', 'GMT': 'UTC', 'Z': 'UTC',
    'CET': 'Europe/Paris', 'CEST': 'Europe/Paris',
    'EET': 'Europe/Bucharest', 'EEST': 'Europe/Bucharest',
    'BST': 'Europe/London', 'WET': 'Europe/Lisbon',
    'EST': 'America/New_York', 'EDT': 'America/New_York',
    'CST': 'America/Chicago', 'CDT': 'America/Chicago',
    'MST': 'America/Denver', 'MDT': 'America/Denver',
    'PST': 'America/Los_Angeles', 'PDT': 'America/Los_Angeles',
}

_TIME_PATTERN = re.compile(r'^\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\s*$', re.IGNORECASE)

_END_OF_DAY = time(23, 59, 59)


def parse_deadline_time(text: str) -> Optional[time]:
    """'17:00', '17.30', '5 PM', '5:30pm' -> time; anything else -> None."""
    match = _TIME_PATTERN.match(text or '')
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or '').lower()
    if meridiem == 'pm' and hour != 12:
        hour += 12
    elif meridiem == 'am' and hour == 12:
        hour = 0
    # 12 PM stays 12 — no adjustment needed
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def parse_deadline_timezone(text: str) -> ZoneInfo:
    """IANA name or known abbreviation -> ZoneInfo; anything else -> UTC.

    Abbreviations in TZ_ABBREVIATIONS take priority over any same-named IANA
    zone (e.g. 'CET' -> Europe/Paris, not the POSIX CET zone).
    """
    cleaned = (text or '').strip()
    if not cleaned:
        return ZoneInfo('UTC')
    # Check our curated abbreviation table first.
    mapped = TZ_ABBREVIATIONS.get(cleaned.upper())
    if mapped:
        return ZoneInfo(mapped)
    # Fall through to IANA lookup for full names like 'Europe/Paris'.
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError):
        pass
    return ZoneInfo('UTC')


def compute_deadline_at(deadline_date: Optional[date],
                        deadline_time: str = '',
                        deadline_timezone: str = '') -> Optional[datetime]:
    """Best-effort deadline moment. No date -> None. Unparseable time ->
    end of day; unparseable timezone -> UTC."""
    if not deadline_date:
        return None
    moment = parse_deadline_time(deadline_time) or _END_OF_DAY
    tz = parse_deadline_timezone(deadline_timezone)
    return datetime.combine(deadline_date, moment, tzinfo=tz)
