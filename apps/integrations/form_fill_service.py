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
    'x_baggage_tag': "the baggage tag number",
    'x_booking_ref': "the booking / confirmation number",
    'x_street_address': "the shipping street address",
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


def build_fill_task(url: str, secrets: dict, facts: dict = None, playbook: str = '',
                    context: str = '') -> str:
    """The fill instruction. References placeholder KEYS only — never the real values.
    The x_* keys are typed literally and Browser Use swaps in the real value; masked
    <...> tokens must NEVER be typed into a field. `facts` are non-PII values shown so
    the agent can choose dropdowns; `playbook` is site-specific guidance for this form."""
    host = next(iter(secrets), '')
    present = secrets.get(host, {})
    lines = [f"- {name}: {_LABELS.get(name, name)}" for name in present]
    fields = "\n".join(lines)
    preamble = (context + "\n\n") if context else ""
    facts = facts or {}
    facts_block = (
        "Known facts about this case (real, non-personal values — use them to choose "
        "dropdowns and matching options):\n"
        + "\n".join(f"- {k}: {v}" for k, v in facts.items()) + "\n\n"
    ) if facts else ""
    playbook_block = (
        "Site-specific guidance for THIS form (follow it):\n" + playbook.strip() + "\n\n"
    ) if (playbook or "").strip() else ""
    return (
        f"{preamble}You are an Airport Lost Found agent filling a lost-item report form "
        f"on the claimant's behalf. Open the form at {url} and fill it in.\n\n"
        f"{facts_block}"
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
        f"matching x_item_description / x_flight_details / x_incident_details values above. Keep "
        f"the wording about this item and this loss only. Do NOT paste long passages from the case "
        f"history, our internal claim or reference IDs, or another company's case or report numbers "
        f"— the institution does not need them.\n\n"
        f"{playbook_block}"
        f"Do NOT invent or infer values — use ONLY what the case details or the known facts above "
        f"state. For a dropdown or required choice with no value available, leave it blank (or choose "
        f"an explicit Unknown/Other option if the form has one). Do NOT reason from outside knowledge "
        f"(for example, which terminal an airline normally uses); never enter a specific value such "
        f"as a terminal that the case details do not state.\n\n"
        f"Leave any field you have no value for blank. If a field's input control is too fiddly to "
        f"operate (for example a custom pop-up picker or a dropdown that will not accept your choice) "
        f"and you cannot fill it after two attempts, leave it and move on rather than retrying "
        f"repeatedly. In your final summary, list any fields you could not fill so a human can "
        f"complete them during review. IMPORTANT: do NOT submit the form — stop once every field you "
        f"can fill is filled, so a human can review it."
    )
