"""Client-update placeholder extraction + per-milestone on-brand templates.

``macro_fields`` returns the dict of values that map onto the Zendesk-macro
placeholders used in client-update messages.  Values are sourced from the live
ticket's custom fields first (when ticket_data is supplied), with claim-model
fallbacks where a matching attribute exists.  Any value not found comes back as
'' so templates can safely omit empty clauses.

``milestone_message`` builds the on-brand email body for a given milestone
(DAY_2, DAY_5, DAY_11, DAY_21, DAY_<n> tail, FINAL) from macro_fields, with
the service period dynamic and graceful handling of missing placeholder values.
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


# ---------------------------------------------------------------------------
# Shared copy blocks (no em-dashes or en-dashes anywhere)
# ---------------------------------------------------------------------------

_STANDARD_DISCLAIMER = (
    "Given the unpredictable nature of lost-and-found operations, we cannot promise "
    "the recovery of every item. We do pledge to search every relevant location "
    "thoroughly. Statistically, only about 72% of lost items are turned in to "
    "recovery points."
)

_SIGN_OFF = "Warm regards,\nThe Airport Lost & Found team"


def _final_disclaimer(period_days: int) -> str:
    return (
        f"Despite our best efforts, we were unable to recover your item within the "
        f"{period_days}-day service period. Because lost items are unpredictable, and "
        f"as we explained at the start, we cannot guarantee retrieval. We sincerely "
        f"appreciate your trust and thank you for choosing our service."
    )


def _greeting(first_name: str) -> str:
    name = first_name.strip() if first_name else ''
    return f"Dear {name}," if name else "Dear there,"


# ---------------------------------------------------------------------------
# Per-milestone template builders (pure functions — no I/O)
# ---------------------------------------------------------------------------

def _day2_body(f: dict, period_days: int) -> list[str]:
    """DAY_2: still searching, extended to TSA, contact if recovery centre calls."""
    lines = []
    item_clause = f" for your {f['lost_item']}" if f['lost_item'] else ""
    lines.append(
        f"We are still searching{item_clause} and wanted to give you a quick update."
    )

    # Collaboration with airport
    if f['airport']:
        lines.append(
            f"We are collaborating closely with {f['airport']} to locate your item."
        )

    # Airline + flight details
    if f['airline'] and f['flight']:
        lines.append(
            f"A detailed description was sent to {f['airline']} using your flight "
            f"{f['flight']} to pinpoint the arriving and departure airports."
        )
    elif f['airline']:
        lines.append(
            f"A detailed description was sent to {f['airline']} to help pinpoint "
            f"the arriving and departure airports."
        )
    elif f['flight']:
        lines.append(
            f"A detailed description was sent using your flight {f['flight']} to "
            f"pinpoint the arriving and departure airports."
        )

    lines.append(
        "Because lost items are unpredictable, we extended the search to TSA at both "
        "airports to maximise the chances of recovery."
    )
    lines.append(
        "We will keep at it and update you as the search progresses."
    )
    lines.append(
        "Our claims process gives the recovery facilities your contact number, full "
        "descriptions, and any photos you provided for fast identification and direct "
        "contact with you."
    )
    lines.append(
        "If any recovery centre contacts you directly, please let us know right away "
        "so we can update the search."
    )
    return lines


def _day5_body(f: dict, period_days: int) -> list[str]:
    """DAY_5: no notification yet, request heads-up if offices have been in touch."""
    lines = []
    item_clause = f" regarding your {f['lost_item']}" if f['lost_item'] else ""
    lines.append(
        f"A quick update{item_clause}: as of now we have had no notification of its "
        f"discovery by phone or email."
    )

    if f['airport'] and f['airline']:
        lines.append(
            f"If {f['airport']} or {f['airline']}, the two main recovery centres we "
            f"contacted, have reached out to you, please give us a heads-up so we can "
            f"update the search accordingly."
        )
    elif f['airport']:
        lines.append(
            f"If {f['airport']}, one of the main recovery centres we contacted, has "
            f"reached out to you, please give us a heads-up so we can update the search."
        )
    elif f['airline']:
        lines.append(
            f"If {f['airline']}, one of the main recovery centres we contacted, has "
            f"reached out to you, please give us a heads-up so we can update the search."
        )
    else:
        lines.append(
            "If any of the recovery centres we contacted have reached out to you "
            "directly, please give us a heads-up so we can update the search."
        )

    lines.append(
        "We provide full descriptions and any photos to all facilities to support "
        "swift identification and direct contact with you."
    )
    lines.append(
        "If a recovery centre contacts you, please let us know so we can update the "
        "ticket. Thank you for your cooperation."
    )
    return lines


def _day11_body(f: dict, period_days: int) -> list[str]:
    """DAY_11: commitment beyond ticket closure, contact details confirmed."""
    lines = []
    item_clause = f" on locating your {f['lost_item']}" if f['lost_item'] else ""
    lines.append(f"Here is an update{item_clause}.")
    lines.append(
        f"Our commitment does not end when the ticket closes on day {period_days}. "
        f"We have located items even after the {period_days}-day mark and we arrange "
        f"shipping once found, even if the ticket is closed on our end."
    )

    # Date + claim reference
    if f['flight_date'] and f['claim_ref']:
        lines.append(
            f"Since {f['flight_date']}, when we received your ALF report {f['claim_ref']}, "
            f"we have confirmed the information for the lost-and-found offices."
        )
    elif f['flight_date']:
        lines.append(
            f"Since {f['flight_date']}, when we received your report, we have confirmed "
            f"the information for the lost-and-found offices."
        )
    elif f['claim_ref']:
        lines.append(
            f"Since we received your ALF report {f['claim_ref']}, we have confirmed "
            f"the information for the lost-and-found offices."
        )
    else:
        lines.append(
            "We have confirmed all information with the lost-and-found offices."
        )

    # Phone reminder
    if f['phone']:
        lines.append(
            f"Your contact number {f['phone']} was included on every report, so please "
            f"keep your phone handy in case a facility reaches out directly."
        )
    else:
        lines.append(
            "Your contact number was included on every report, so please keep your "
            "phone handy in case a facility reaches out directly."
        )

    # Airport + airline holding details
    if f['airport'] and f['airline']:
        lines.append(
            f"{f['airport']} and {f['airline']} both hold your contact details and a "
            f"full description, so it is a matter of the item reaching a desk and you "
            f"hearing from them."
        )
    elif f['airport']:
        lines.append(
            f"{f['airport']} holds your contact details and a full description, so it "
            f"is a matter of the item reaching a desk and you hearing from them."
        )
    elif f['airline']:
        lines.append(
            f"{f['airline']} holds your contact details and a full description, so it "
            f"is a matter of the item reaching a desk and you hearing from them."
        )
    else:
        lines.append(
            "The recovery offices hold your contact details and a full description, "
            "so it is a matter of the item reaching a desk and you hearing from them."
        )

    lines.append(
        "If you have already had a call about your item, please email us so we can "
        "update the ticket and stop the search elsewhere. Feel free to reach out "
        "anytime."
    )
    return lines


def _day21_body(f: dict, elapsed_days: int, period_days: int) -> list[str]:
    """DAY_21 and the tail (DAY_31+): still missing, determination continues."""
    lines = []
    item_clause = f" on your {f['lost_item']}" if f['lost_item'] else ""
    lines.append(f"Here is the latest update{item_clause}.")
    lines.append(
        f"We have now passed {elapsed_days} days of effort and the item is still "
        f"missing, but our determination continues."
    )
    lines.append(
        "Your satisfaction and the return of your item remain our priority, and we "
        "appreciate your patience."
    )
    lines.append(
        f"Our dedication extends beyond ticket closure. We have retrieved items even "
        f"after the {period_days}-day timeframe and we arrange shipping once found, "
        f"regardless of ticket status."
    )
    return lines


def _final_body(f: dict, period_days: int) -> list[str]:
    """FINAL closer: honest conclusion, door left open."""
    lines = []
    item_clause = f" your {f['lost_item']}" if f['lost_item'] else " your lost item"
    lines.append(
        f"During our {period_days}-day investigation into{item_clause} we sent "
        f"detailed reports, including your contact number, to all relevant recovery "
        f"points."
    )
    lines.append(
        "Despite our efforts and the feedback received, the item has not been located."
    )
    lines.append(
        "Recovery points may contact customers directly by phone when an item is "
        "found, so if any reached you during the investigation please let us know."
    )
    lines.append(
        f"If not, per our protocol we have reached the conclusion of our search for"
        f"{item_clause} and we are sorry for the inconvenience."
    )
    lines.append(
        "We exhausted every avenue in our search."
    )
    lines.append(
        "Although we are closing this ticket, our commitment to your satisfaction "
        "remains and you can reach out to us anytime."
    )
    lines.append(
        "Thank you for your patience and trust."
    )
    return lines


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

def milestone_message(claim, milestone: str, ticket_data=None, period_days: int = 30) -> str:
    """Build the on-brand client-update body for a milestone, filled from
    macro_fields(claim, ticket_data), period-dynamic, graceful on empty fields.

    DAY_2/5/11/21 use their own templates; any other DAY_<n> (the tail, e.g.
    DAY_31, DAY_41) uses the still-searching template with elapsed_days from the
    milestone number; FINAL uses the closer.

    No em-dashes or en-dashes appear in any output.  Missing placeholder values
    cause the relevant clause/sentence to be omitted rather than printing a blank.
    """
    f = macro_fields(claim, ticket_data)
    greeting = _greeting(f['first_name'])

    # Compute elapsed_days: DAY_<n> -> n; FINAL -> period_days
    elapsed_days = period_days
    if milestone and milestone.startswith('DAY_'):
        try:
            elapsed_days = int(milestone[4:])
        except ValueError:
            elapsed_days = period_days

    # Dispatch to the right body builder
    if milestone == 'DAY_2':
        body_lines = _day2_body(f, period_days)
        disclaimer = _STANDARD_DISCLAIMER
    elif milestone == 'DAY_5':
        body_lines = _day5_body(f, period_days)
        disclaimer = _STANDARD_DISCLAIMER
    elif milestone == 'DAY_11':
        body_lines = _day11_body(f, period_days)
        disclaimer = _STANDARD_DISCLAIMER
    elif milestone == 'DAY_21':
        body_lines = _day21_body(f, elapsed_days, period_days)
        disclaimer = _STANDARD_DISCLAIMER
    elif milestone == 'FINAL':
        body_lines = _final_body(f, period_days)
        disclaimer = _final_disclaimer(period_days)
    else:
        # Tail milestones (DAY_31, DAY_41, …) use the still-searching copy
        body_lines = _day21_body(f, elapsed_days, period_days)
        disclaimer = _STANDARD_DISCLAIMER

    # Assemble: greeting + blank + body paragraphs (each its own paragraph) +
    # blank + disclaimer + blank + sign-off.
    paragraphs = [greeting, ''] + body_lines + ['', disclaimer, '', _SIGN_OFF]
    return '\n'.join(paragraphs)
