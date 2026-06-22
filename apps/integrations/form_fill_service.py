"""Build the Browser Use task + domain-scoped secrets from a claim. The agent
(LLM) only ever sees placeholder NAMES (x_client_name, …); the real values are
filled into the form by Browser Use, never sent to the model."""
from urllib.parse import urlparse

# placeholder name -> Claim attribute
_FIELD_MAP = [
    ('x_client_name', 'client_name'),
    ('x_client_email', 'email_alias'),   # use the per-ticket alias so replies route back via LORA
    ('x_client_phone', 'phone'),
    ('x_item_description', 'object_description'),
    ('x_lost_location', 'lost_location'),
    ('x_flight_details', 'flight_details'),
    ('x_incident_details', 'incident_details'),
    ('x_claim_ref', 'alf_claim_id'),
]

_LABELS = {
    'x_client_name': "the claimant's full name",
    'x_client_email': "the claimant's contact email — use this EXACT address so replies route back to us",
    'x_client_phone': "the claimant's phone",
    'x_item_description': "the lost item's description",
    'x_lost_location': "where the item was lost",
    'x_flight_details': "the flight details",
    'x_incident_details': "how/when it was lost",
    'x_claim_ref': "the claim reference number",
}

SUBMIT_TASK = ("Submit the form now by clicking its submit/send button, then report "
               "the confirmation message or reference shown.")


def form_host(url: str) -> str:
    return (urlparse(url).hostname or '').lower()


def build_form_secrets(claim, host: str) -> dict:
    """Return {host: {placeholder: value}} for every non-empty claim field."""
    values = {}
    for placeholder, attr in _FIELD_MAP:
        val = (getattr(claim, attr, '') or '')
        val = str(val).strip()
        if val:
            values[placeholder] = val
    return {host: values}


def build_agent_context(claim, ticket_data: dict) -> str:
    """Business context + the ticket conversation, with the client's identifying PII
    MASKED (names/emails/phones tokenized) before it reaches Browser Use's LLM. Gives
    the agent the full case so it can fill descriptive form fields; the actual contact
    values are filled separately via the secrets channel."""
    from apps.integrations.services import build_ticket_thread
    from apps.integrations.briefing import ALF_BUSINESS_CONTEXT, normalize_fetched_comments
    from apps.ai.client import _build_tokenizer

    raw_comments = ticket_data.get('comments', [])
    # normalize_fetched_comments handles the {author: dict, body: str} shape from
    # fetch_zendesk_comments, as well as already-normalised {text: str} dicts.
    normalized_comments = normalize_fetched_comments(raw_comments)

    thread = build_ticket_thread({
        'subject': ticket_data.get('subject', ''),
        'description': ticket_data.get('description', ''),
        'ticket_created_at': ticket_data.get('created_at', '') or ticket_data.get('ticket_created_at', ''),
        'comments': normalized_comments,
    })
    parts = []
    if thread.get('ticket_subject'):
        parts.append('Subject: ' + thread['ticket_subject'])
    if thread.get('ticket_description'):
        parts.append('Description: ' + thread['ticket_description'])
    for line in thread.get('zendesk_comment', []):
        parts.append(line)
    history = '\n'.join(parts).strip()
    if history:
        known_pii = {
            'aliases': [a for a in [getattr(claim, 'email_alias', ''),
                                     getattr(claim, 'client_email', '')] if a],
            'names': [n for n in [getattr(claim, 'client_name', '')] if n],
        }
        try:
            history = _build_tokenizer(known_pii).tokenize(history, {})
        except Exception:
            history = ''   # if masking fails, send NO raw history (fail safe for PII)
    if not history:
        return ALF_BUSINESS_CONTEXT
    return (ALF_BUSINESS_CONTEXT
            + '\n\nTICKET HISTORY for this case (personal identifiers are masked as '
            + '<NAME_..>/<EMAIL_..>/<PHONE_..> placeholders):\n' + history)


def build_fill_task(url: str, secrets: dict, context: str = '') -> str:
    """The fill instruction. References placeholders only — never the real values."""
    host = next(iter(secrets), '')
    present = secrets.get(host, {})
    lines = [f"- {name}: {_LABELS.get(name, name)}" for name in present]
    fields = "\n".join(lines)
    preamble = (context + "\n\n") if context else ""
    return (
        f"{preamble}You are an Airport Lost Found agent filling a lost-item report form "
        f"on the claimant's behalf. Open the form at {url} and fill it in.\n"
        f"Use these secret placeholder values for the matching fields (match by the form's "
        f"own field labels):\n{fields}\n"
        f"For the contact email field, use x_client_email exactly (it routes the institution's "
        f"reply back to us). Use the TICKET HISTORY above to fill any DESCRIPTIVE fields the "
        f"form has — item description, identifying marks, circumstances of loss — as completely "
        f"as the history allows. Leave any field you have no value for blank. IMPORTANT: do NOT "
        f"submit the form — stop once every field you can fill is filled, so a human can review it."
    )
