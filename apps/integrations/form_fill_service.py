"""Build the Browser Use task + domain-scoped secrets from a claim. The agent
(LLM) only ever sees placeholder NAMES (x_client_name, …); the real values are
filled into the form by Browser Use, never sent to the model."""
from urllib.parse import urlparse

# Cost guard: stop a fill that blows past this many agent steps. Each step is a
# billed LLM call, so a runaway (e.g. stuck looping on a control) grinds up the
# bill; a clean fill is well under this. Tunable.
MAX_FILL_STEPS = 60

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
    'x_client_name': "the claimant's full name (use for a single full-name box)",
    'x_client_first_name': "the claimant's first name (use for a separate First name box)",
    'x_client_last_name': "the claimant's last/family name (use for a separate Last name box)",
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


def _split_name(full_name: str):
    """Split a full name into (first, last) for forms with separate boxes. A single
    token -> first only. Best-effort; the human review catches odd splits."""
    parts = (full_name or '').strip().split()
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], ' '.join(parts[1:])


def build_form_secrets(claim, host: str) -> dict:
    """Return {host: {placeholder: value}} for every non-empty claim field. Also
    derives first/last name so forms with separate First/Last boxes can be filled
    (the full name stays available for single-box forms)."""
    values = {}
    for placeholder, attr in _FIELD_MAP:
        val = (getattr(claim, attr, '') or '')
        val = str(val).strip()
        if val:
            values[placeholder] = val
    first, last = _split_name(getattr(claim, 'client_name', ''))
    if first:
        values['x_client_first_name'] = first
    if last:
        values['x_client_last_name'] = last
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
    """The fill instruction. References placeholder KEYS only — never the real values.
    The x_* keys are typed literally and Browser Use swaps in the real value; the
    masked <...> tokens from the case history must NEVER be typed into a field."""
    host = next(iter(secrets), '')
    present = secrets.get(host, {})
    lines = [f"- {name}: {_LABELS.get(name, name)}" for name in present]
    fields = "\n".join(lines)
    preamble = (context + "\n\n") if context else ""
    return (
        f"{preamble}You are an Airport Lost Found agent filling a lost-item report form "
        f"on the claimant's behalf. Open the form at {url} and fill it in.\n\n"
        f"HOW TO ENTER VALUES — read carefully:\n"
        f"Type these secret keys EXACTLY as written; the system swaps in the real value as you "
        f"type. Match each to the form field by its label:\n{fields}\n"
        f"- For a single full-name box use x_client_name; for separate First/Last boxes use "
        f"x_client_first_name and x_client_last_name.\n"
        f"- For the contact email box type x_client_email exactly — it routes the institution's "
        f"reply back to us.\n"
        f"- NEVER type a masked placeholder such as <NAME_...>, <ALIAS_...>, <PHONE_...> or "
        f"<ALF_ID_...> into any form field. Those are masks shown only so you understand the case; "
        f"they are not real data. Disregard any earlier instruction about repeating placeholders "
        f"verbatim — that is for writing notes, not for filling forms. If the only value you have "
        f"for a field is a <...> placeholder, leave that field blank.\n\n"
        f"DESCRIPTIVE fields (item description, identifying marks, where/how it was lost): use the "
        f"matching x_item_description / x_lost_location / x_flight_details / x_incident_details "
        f"values above. Keep the wording about this item and this loss only. Do NOT paste long "
        f"passages from the case history, our internal claim or reference IDs, or another company's "
        f"case or report numbers — the institution does not need them.\n\n"
        f"Do NOT invent values. For a dropdown or required choice with no matching real value, pick "
        f"the closest sensible option or leave it for the human; never guess a specific value (such "
        f"as a terminal) the data does not support.\n\n"
        f"Leave any field you have no value for blank. If a field's input control is too fiddly to "
        f"operate (for example a custom pop-up picker or a dropdown that will not accept your choice) "
        f"and you cannot fill it after two attempts, leave it and move on rather than retrying "
        f"repeatedly. In your final summary, list any fields you could not fill so a human can "
        f"complete them during review. IMPORTANT: do NOT submit the form — stop once every field you "
        f"can fill is filled, so a human can review it."
    )
