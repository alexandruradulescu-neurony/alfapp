"""Build the Browser Use task + domain-scoped secrets from a claim. The agent
(LLM) only ever sees placeholder NAMES (x_client_name, …); the real values are
filled into the form by Browser Use, never sent to the model."""
from urllib.parse import urlparse

# placeholder name -> Claim attribute
_FIELD_MAP = [
    ('x_client_name', 'client_name'),
    ('x_client_email', 'client_email'),
    ('x_client_phone', 'phone'),
    ('x_item_description', 'object_description'),
    ('x_lost_location', 'lost_location'),
    ('x_flight_details', 'flight_details'),
    ('x_incident_details', 'incident_details'),
    ('x_claim_ref', 'alf_claim_id'),
]

_LABELS = {
    'x_client_name': "the claimant's full name",
    'x_client_email': "the claimant's email",
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


def build_fill_task(url: str, secrets: dict) -> str:
    """The fill instruction. References placeholders only — never the real values."""
    host = next(iter(secrets), '')
    present = secrets.get(host, {})
    lines = [f"- {name}: {_LABELS.get(name, name)}" for name in present]
    fields = "\n".join(lines)
    return (
        f"Open the lost-item report form at {url} and fill it in. Use these secret "
        f"placeholder values for the matching fields (match by the form's own field "
        f"labels):\n{fields}\n"
        f"Leave any field you have no value for blank. IMPORTANT: do NOT submit the "
        f"form — stop once every field you can fill is filled, so a human can review it."
    )
