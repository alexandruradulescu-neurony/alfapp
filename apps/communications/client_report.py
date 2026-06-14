"""Client "what we did" update — drafted when a claim enters the configured
submitted-status, reviewed by an agent, and sent as a public Zendesk reply.

Deterministic on-brand template (never promises recovery) + optional light AI
polish of the wording. PII is masked before the LLM sees anything; the client's
name is restored on the way out via the AIClient's reverse-tokenization."""

import logging

logger = logging.getLogger(__name__)

CLIENT_REPORT_SYSTEM_PROMPT = (
    "You are a support agent at Airport Lost & Found, a paid lost-item recovery "
    "service. Polish the following client update message so it reads warm, "
    "reassuring, professional and concise. Rules: keep EVERY fact exactly as "
    "given; do NOT add any new facts or claims; NEVER promise, guarantee or "
    "imply that the item will be found or recovered; keep a greeting and a "
    "sign-off from 'The Airport Lost & Found team'. Return only the final "
    "message body."
)


def _first_line(text: str, limit: int = 120) -> str:
    if not text:
        return ''
    return text.strip().splitlines()[0][:limit]


def _flight_phrase(claim) -> str:
    fd = getattr(claim, 'flight_data', None) or {}
    number = (fd.get('number') or '').strip()
    airline = (fd.get('airline') or '').strip()
    if number and airline:
        return f", including your flight {airline} {number}"
    if number:
        return f", including your flight {number}"
    return ''


def _parties_phrase(claim) -> str:
    """Name the airport (from lost location) and airline where we know them."""
    parties = []
    loc = (getattr(claim, 'lost_location', '') or '').strip()
    if loc:
        # lost_location is often "Airport / CODE" or free text — keep it short.
        parties.append(_first_line(loc, 80))
    airline = ((getattr(claim, 'flight_data', None) or {}).get('airline') or '').strip()
    if airline:
        parties.append(airline)
    if parties:
        return " (including " + " and ".join(parties) + ")"
    return ''


def build_client_update_template(claim) -> str:
    """The deterministic, on-brand client update — no AI, no over-promising."""
    name = (getattr(claim, 'client_name', '') or '').strip() or 'there'
    obj = _first_line(getattr(claim, 'object_description', '') or '') or 'your lost item'
    ref = (getattr(claim, 'alf_claim_id', '') or '').strip()
    flight = _flight_phrase(claim)
    parties = _parties_phrase(claim)

    lines = [
        f"Dear {name},",
        "",
        f"Thank you for trusting Airport Lost & Found with the search for your {obj}. "
        "We wanted to update you on the work we have completed so far.",
        "",
        f"• We reviewed the details you provided and confirmed the specifics of your case{flight}.",
        f"• We have formally reported your lost item to the relevant lost-and-found offices{parties}, "
        "on your behalf.",
    ]
    if ref:
        lines.append(f"• Your case is being tracked under reference {ref}.")
    lines += [
        "",
        "What happens next: lost-and-found recovery can take time. We will continue to follow up with "
        "the offices involved and will keep you updated as soon as we hear anything.",
        "",
        "If you remember any further details about your item, simply reply to this message — even small "
        "details can help our search.",
        "",
        "Kind regards,",
        "The Airport Lost & Found team",
    ]
    return "\n".join(lines)


def _known_pii_for(claim) -> dict:
    names = [getattr(claim, 'client_name', ''), getattr(claim, 'billing_address', ''),
             getattr(claim, 'shipping_address', '')]
    aliases = [getattr(claim, 'client_email', ''), getattr(claim, 'email_alias', ''),
               getattr(claim, 'alternate_email', ''), getattr(claim, 'phone', '') or '']
    return {
        'names': [str(n).strip() for n in names if n and str(n).strip()],
        'aliases': [str(a).strip() for a in aliases if a and str(a).strip()],
    }


def build_client_update_message(claim, polish: bool = True) -> str:
    """Build the client update: the template, optionally polished by the LLM.
    Always returns a usable message — AI failure/absence falls back to the
    template. PII is masked before the LLM and restored on the way out."""
    template = build_client_update_template(claim)
    if not polish:
        return template
    try:
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        if not getattr(ss, 'ai_api_key', ''):
            return template
        from apps.ai.client import AIClient
        from apps.ai.schemas import EmailDraft
        result = AIClient.complete(
            system_prompt=CLIENT_REPORT_SYSTEM_PROMPT,
            trusted={'draft_to_polish': template},
            untrusted={},
            known_pii=_known_pii_for(claim),
            response_schema=EmailDraft,
            call_site='client_update',
            temperature=0.4,
            max_tokens=900,
        )
        body = (result.body or '').strip()
        return body or template
    except Exception as e:
        logger.warning(f"Client-update AI polish unavailable for claim #{getattr(claim, 'id', '?')}; "
                       f"using template: {e}")
        return template
