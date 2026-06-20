"""Claim-domain pure helpers (no model imports — safe for migrations)."""
import re
from datetime import date, datetime, time
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Evidence-upload limits — shared so the API and the frontend agree on what an
# acceptable image is (the API path previously did no validation at all).
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
EVIDENCE_ALLOWED_EXTENSIONS = ('jpg', 'jpeg', 'png', 'gif', 'webp')
# Dispute submissions may also ship PDF documents to PayPal (claim evidence
# stays image-only — see validate_evidence_image vs validate_evidence_attachment).
EVIDENCE_DOC_ALLOWED_EXTENSIONS = EVIDENCE_ALLOWED_EXTENSIONS + ('pdf',)


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
    # Don't trust the name/extension — confirm the bytes actually decode as an
    # image (arbitrary bytes named .png are otherwise accepted and later sent to
    # PayPal / stored). Rewind afterwards so the real save reads from the start.
    try:
        from PIL import Image, UnidentifiedImageError
    except Exception:
        return  # Pillow unavailable — fall back to size+extension only
    try:
        image.seek(0)
        Image.open(image).verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValidationError('File is not a valid image (it could not be decoded).')
    finally:
        try:
            image.seek(0)
        except Exception:
            pass


def validate_evidence_attachment(f) -> None:
    """Size + type validation for a DISPUTE-submission attachment: the evidence
    images PayPal accepts PLUS PDF documents. Image extensions go through the
    full image-decode check (via validate_evidence_image); a PDF is checked by
    size, extension and a %PDF magic-byte sniff (don't trust the name). Raises
    django.core.exceptions.ValidationError on failure. Pure (no model imports).

    Claim evidence stays image-only (validate_evidence_image); only disputes
    need to send PDFs onward to PayPal."""
    from django.core.exceptions import ValidationError
    if f is None:
        raise ValidationError('A file is required.')
    if f.size > EVIDENCE_MAX_BYTES:
        raise ValidationError(f'File must be under {EVIDENCE_MAX_BYTES // 1024 // 1024}MB.')
    ext = f.name.rsplit('.', 1)[-1].lower() if '.' in (f.name or '') else ''
    if ext not in EVIDENCE_DOC_ALLOWED_EXTENSIONS:
        raise ValidationError(
            f'Invalid file type. Allowed: {", ".join(EVIDENCE_DOC_ALLOWED_EXTENSIONS)}.')
    if ext != 'pdf':
        validate_evidence_image(f)  # images: full decode verification
        return
    # PDF: confirm the bytes actually start with the PDF marker.
    try:
        f.seek(0)
        head = f.read(5)
    except Exception:
        head = b''
    finally:
        try:
            f.seek(0)
        except Exception:
            pass
    if not head.startswith(b'%PDF'):
        raise ValidationError('File is not a valid PDF (it could not be read as one).')

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


# Fields refreshed from a Zendesk ticket. OVERWRITE = Zendesk is the source of
# truth (structured fields replace the claim value); FILL_ONLY = LLM-inferred
# values that only populate a blank. claim.status is deliberately NOT here — the
# webhook owns the stage mirror.
OVERWRITE_FIELDS = [
    'client_email', 'client_name', 'flight_details', 'phone',
    'billing_address', 'shipping_address', 'incident_details',
    'lost_location', 'deadline_time', 'deadline_timezone',
    'payment_method', 'payment_status', 'woocommerce_id', 'tracking_info',
]
FILL_ONLY_FIELDS = [
    'object_description',
    'alternate_email',  # extractor returns '' today — reserved for when it adds it
]


def refresh_claim_from_zendesk(claim, extracted: dict) -> list:
    """Merge re-extracted ticket facts into `claim` and save it.

    OVERWRITE_FIELDS replace the claim value (Zendesk is the source of truth);
    FILL_ONLY_FIELDS populate blanks only; deadline_date/price_paid are coerced
    and applied if changed; deadline_at is recomputed. Never touches claim.status.
    Returns the list of field names actually changed. Operates on the passed-in
    instance (no model import), so this module stays migration-safe.
    """
    from apps.integrations.services import safe_date, safe_decimal

    updated_fields = []
    for field in OVERWRITE_FIELDS:
        value = (extracted.get(field) or '').strip()
        if field == 'client_email':
            # client_email is a case-sensitive match key; normalize like the API path.
            value = value.lower()
        if value and value != (getattr(claim, field) or ''):
            setattr(claim, field, value)
            updated_fields.append(field)
    for field in FILL_ONLY_FIELDS:
        value = (extracted.get(field) or '').strip()
        if value and not (getattr(claim, field) or ''):
            setattr(claim, field, value)
            updated_fields.append(field)

    new_date = safe_date(extracted.get('deadline_date', ''))
    if new_date and new_date != claim.deadline_date:
        claim.deadline_date = new_date
        updated_fields.append('deadline_date')
    new_price = safe_decimal(extracted.get('price_paid', ''))
    if new_price is not None and new_price != claim.price_paid:
        claim.price_paid = new_price
        updated_fields.append('price_paid')

    claim.deadline_at = compute_deadline_at(
        claim.deadline_date, claim.deadline_time, claim.deadline_timezone)
    save_fields = set(updated_fields) | {'deadline_at', 'updated_at'}
    claim.save(update_fields=list(save_fields))
    return updated_fields
