"""Client-update placeholder extraction.

`macro_fields` returns the dict of values that map onto the Zendesk-macro
placeholders used in client-update messages.  Values are sourced from the live
ticket's custom fields first (when ticket_data is supplied), with claim-model
fallbacks where a matching attribute exists.  Any value not found comes back as
'' so templates can safely omit empty clauses.

A second module responsibility (per-milestone message templates) is added in a
later task — they live here too, alongside macro_fields.
"""

from __future__ import annotations

from apps.integrations.services import (
    _get_custom_field_value,
    ZENDESK_FIELD_AIRPORT,
    ZENDESK_FIELD_AIRLINE,
    ZENDESK_FIELD_FLIGHT,
    ZENDESK_FIELD_DATETIME,
)
from apps.communications.client_report import _first_line


def macro_fields(claim, ticket_data=None) -> dict:
    """Values for the client-update placeholders, matching the Zendesk macros.

    Sourced from the live ticket custom fields when ticket_data is given, with
    Claim fallbacks.  Missing values come back as '' (templates omit empty
    clauses).

    Keys:
        first_name   – first whitespace token of claim.client_name
        lost_item    – first line of claim.object_description
        airport      – from ticket custom field; no claim fallback
        airline      – from ticket custom field; falls back to claim.flight_data['airline']
        flight       – from ticket custom field; falls back to claim.flight_data['number']
        flight_date  – from ticket custom field; no claim fallback
        claim_ref    – claim.alf_claim_id
        phone        – claim.phone
    """
    # --- first_name ---
    raw_name = (getattr(claim, 'client_name', '') or '').strip()
    first_name = raw_name.split()[0] if raw_name.split() else ''

    # --- lost_item ---
    lost_item = _first_line(getattr(claim, 'object_description', '') or '')

    # --- custom fields from live ticket ---
    custom_fields: list = []
    if ticket_data is not None:
        raw = ticket_data.get('custom_fields')
        if isinstance(raw, (list, tuple)):
            custom_fields = raw

    airport = _get_custom_field_value(custom_fields, ZENDESK_FIELD_AIRPORT)
    airline = _get_custom_field_value(custom_fields, ZENDESK_FIELD_AIRLINE)
    flight = _get_custom_field_value(custom_fields, ZENDESK_FIELD_FLIGHT)
    flight_date = _get_custom_field_value(custom_fields, ZENDESK_FIELD_DATETIME)

    # --- claim fallbacks for airline / flight (no ticket_data or field missing) ---
    fd = (getattr(claim, 'flight_data', None) or {})
    if not airline:
        airline = (fd.get('airline') or '').strip()
    if not flight:
        flight = (fd.get('number') or '').strip()

    # --- claim_ref and phone ---
    claim_ref = (getattr(claim, 'alf_claim_id', '') or '').strip()
    phone = (getattr(claim, 'phone', '') or '').strip()

    return {
        'first_name': first_name,
        'lost_item': lost_item,
        'airport': airport,
        'airline': airline,
        'flight': flight,
        'flight_date': flight_date,
        'claim_ref': claim_ref,
        'phone': phone,
    }
