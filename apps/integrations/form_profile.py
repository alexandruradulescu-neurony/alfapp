"""Turn a messy, masked ticket into a clean structured form profile, then split it
into Browser Use secrets (PII / free-text) vs visible facts (non-PII / dropdowns).

One AIClient call does the structuring: the model only ever sees masked text, and
AIClient.complete returns the validated FormProfile with every string field already
UNTOKENIZED, so real values (baggage tag, identifying marks) are recovered server-side
without the model seeing them. A non-dispute call_site routes to DeepSeek (cheap)."""
import logging

from pydantic import BaseModel

from apps.ai.client import AIClient
from apps.integrations.briefing import normalize_fetched_comments
from apps.integrations.services import build_ticket_thread

logger = logging.getLogger(__name__)


class FormProfile(BaseModel):
    """The ~dozen facts a lost-item form actually needs. All optional."""
    # claimant (PII -> secrets)
    first_name: str = ''
    last_name: str = ''
    email_alias: str = ''
    phone: str = ''
    # item
    item_type: str = ''           # visible (feeds a type dropdown)
    item_description: str = ''    # secret (full clean description incl. brand/colour/marks)
    # loss
    airport: str = ''             # visible
    airline: str = ''             # visible
    flight: str = ''              # secret (masked category)
    lost_date: str = ''           # visible (MM/DD/YYYY if known)
    where_lost: str = ''          # visible (short category)
    how_lost: str = ''            # secret (incident narrative)
    # ids (secrets)
    baggage_tag: str = ''
    booking_confirmation: str = ''
    claim_ref: str = ''
    # address
    street: str = ''              # secret
    city: str = ''                # visible
    state: str = ''               # visible
    zip: str = ''                 # visible
    country: str = ''             # visible


SYSTEM_PROMPT = (
    "You extract a structured lost-item form profile from an airport lost-and-found case. "
    "Return ONLY fields you can support from the case text; leave the rest blank. "
    "item_description: one tidy sentence with brand, colour and identifying marks. "
    "how_lost: one tidy sentence on the circumstances. lost_date as MM/DD/YYYY if stated. "
    "Do not invent values. Copy any <NAME_..>/<PHONE_..>/<ALIAS_..>/<ALF_ID_..> placeholders "
    "verbatim into the matching field — they are resolved to real values later."
)

# secret placeholder key -> FormProfile attr (real values typed verbatim into free-text fields)
_SECRET_FIELDS = [
    ('x_client_first_name', 'first_name'), ('x_client_last_name', 'last_name'),
    ('x_client_email', 'email_alias'), ('x_client_phone', 'phone'),
    ('x_item_description', 'item_description'), ('x_incident_details', 'how_lost'),
    ('x_flight_details', 'flight'), ('x_baggage_tag', 'baggage_tag'),
    ('x_booking_ref', 'booking_confirmation'), ('x_claim_ref', 'claim_ref'),
    ('x_street_address', 'street'),
]
# visible label -> FormProfile attr (non-PII; shown to the agent so it can pick dropdowns)
_VISIBLE_FIELDS = [
    ('Item type', 'item_type'), ('Airport', 'airport'), ('Airline', 'airline'),
    ('Date of loss', 'lost_date'), ('Where lost', 'where_lost'),
    ('City', 'city'), ('State', 'state'), ('Zip', 'zip'), ('Country', 'country'),
]


def _case_text(claim, ticket_data: dict) -> str:
    thread = build_ticket_thread({
        'subject': ticket_data.get('subject', ''),
        'description': ticket_data.get('description', ''),
        'ticket_created_at': ticket_data.get('created_at', '') or ticket_data.get('ticket_created_at', ''),
        'comments': normalize_fetched_comments(ticket_data.get('comments', [])),
    })
    parts = []
    if thread.get('ticket_subject'):
        parts.append('Subject: ' + thread['ticket_subject'])
    if thread.get('ticket_description'):
        parts.append('Description: ' + thread['ticket_description'])
    parts.extend(thread.get('zendesk_comment', []))
    return '\n'.join(parts).strip()


def build_form_profile(claim, ticket_data: dict):
    """Return a FormProfile (real values, untokenized) or None on failure — the caller
    falls back to raw Claim fields so a fill is never blocked."""
    case = _case_text(claim, ticket_data)
    if not case:
        return None
    known_pii = {
        'aliases': [a for a in [getattr(claim, 'email_alias', ''),
                                getattr(claim, 'client_email', '')] if a],
        'names': [n for n in [getattr(claim, 'client_name', '')] if n],
    }
    try:
        return AIClient().complete(
            system_prompt=SYSTEM_PROMPT,
            trusted={'case': case},
            known_pii=known_pii,
            response_schema=FormProfile,
            call_site='form_fill_profile',
        )
    except Exception as e:   # noqa: BLE001 — never block a fill on a structuring failure
        logger.warning('Form profile build failed for claim %s: %s', getattr(claim, 'pk', '?'), e)
        return None


def profile_to_secrets_and_facts(profile: FormProfile):
    """Split a profile into (secrets {x_*: real_value}, facts {Label: real_value}).
    A '<...>' value (an untokenize miss) is dropped from secrets so a mask never
    reaches the form."""
    secrets = {}
    for key, attr in _SECRET_FIELDS:
        val = str(getattr(profile, attr, '') or '').strip()
        if val and not val.startswith('<'):
            secrets[key] = val
    facts = {}
    for label, attr in _VISIBLE_FIELDS:
        val = str(getattr(profile, attr, '') or '').strip()
        if val and not val.startswith('<'):
            facts[label] = val
    return secrets, facts
