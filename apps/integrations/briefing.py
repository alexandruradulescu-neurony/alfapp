"""Shared AI business context + the claim summary engine.

The stored claim summary (claim.ai_summary) is written ONLY here — by the
Zendesk webhook (creation + status change) and the "Refresh from Zendesk"
view. The sidebar briefing endpoint shares the business context but stays
read-only (no stored-summary writes from agent clicks). All AI calls go
through apps/ai/AIClient (PII tokenization — never a passthrough)."""
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from django.utils import timezone

from apps.ai.client import AIClient
from apps.ai.schemas import BriefingSummary
from apps.integrations.services import build_claim_facts, build_ticket_thread

if TYPE_CHECKING:
    from apps.claims.models import Claim

logger = logging.getLogger(__name__)

# Most recent ticket comments fed into the claim-summary AI context.
MAX_THREAD_COMMENTS = 30

# AI-tuning defaults for the claim-summary call (kept named so summary behaviour
# is tweaked in one place).
SUMMARY_TEMPERATURE = 0.4
SUMMARY_MAX_TOKENS = 500

STATUS_VOCABULARY = (
    "Zendesk workflow statuses (the claim's status uses these exact names): "
    "'New' and 'Open' = intake, not yet worked. 'Investigation initiated' = ALF staff "
    "working the case (client sees 'Open'). 'Claim submitted' = loss reports filed with "
    "the airport/airline/security institutions (client sees 'Search in progress'). "
    "'Object Found' = item located; retrieval or shipping underway. 'Pending' = waiting "
    "for the client to reply. 'Refund Requested' = client asked for a refund; management "
    "decision pending. 'Refund-Denied' = refund denied after confirming with the client; "
    "the case is closing. 'Solved' and 'Solved - Object Found' = case ended successfully. "
    "'Closed - Object Not Found' = search failed and the case is closed. "
    "'Closed - Client Not Answering' = closed because the client stopped responding. "
    "'Closed - Refunded' = closed with a refund. "
)

ALF_BUSINESS_CONTEXT = (
    "Airport Lost Found (ALF) is a paid concierge service: travelers who lost an item "
    "at an airport or on a flight pay ALF to run the recovery for them. Clients submit "
    "a web form and never email; ALF reports the loss to the airport, airline and "
    "security (TSA) lost-and-found offices and then corresponds with those institutions "
    "by email and phone. Inbound emails come from institutions, not clients. Case "
    "lifecycle: reported -> searching -> found or not found -> retrieval (client pickup, "
    "authorized person, or courier/UPS at the client's expense) -> delivered -> closed. "
    "ALF has no staff at airports and cannot search physically — it works by reporting, "
    "calling and emailing. Comments marked 'internal note' are ALF staff notes; 'public' "
    "ones are visible to the client. Comments are listed in chronological order. Person "
    "names and contact details may appear as <NAME_...>/<EMAIL_...>/<PHONE_...> "
    "placeholders — treat each placeholder as that person or value and repeat it "
    "verbatim when referring to them, INCLUDING the angle brackets "
    "(write <NAME_12ab34cd>, never NAME_12ab34cd). "
) + STATUS_VOCABULARY

SUMMARY_PROMPT = ALF_BUSINESS_CONTEXT + (
    "Write a management summary of at most 4 sentences for this lost-item case. "
    "Keep it under 600 characters. "
    "Lead with the current workflow status and what it means for the case, then "
    "the key facts (what was lost, where, search position), then what is "
    "currently awaited and from whom. Use ONLY facts present in the provided "
    "content; never invent dates, people or procedures. "
    'Respond as JSON: {"summary": "..."}.'
)


def normalize_fetched_comments(comments: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Server-fetched Zendesk comments ({author:{name}, body, public,
    created_at}) -> the dict shape build_ticket_thread renders."""
    normalized = []
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        author = c.get('author')
        if isinstance(author, dict):
            author = author.get('name', '')
        normalized.append({
            'author': str(author or ''),
            'created_at': str(c.get('created_at', '') or ''),
            'public': c.get('public', True),
            'text': str(c.get('body', '') or c.get('text', '') or ''),
        })
    return normalized


def generate_claim_summary(claim: 'Claim', ticket_data: dict) -> Optional[str]:
    """One AI summary of the case, or None on any AI failure (callers must
    treat the summary as optional — a stage change never depends on it)."""
    facts = build_claim_facts(claim)
    untrusted = build_ticket_thread({
        'subject': ticket_data.get('subject', ''),
        'description': ticket_data.get('description', ''),
        'ticket_created_at': ticket_data.get('created_at', ''),
        'comments': normalize_fetched_comments(ticket_data.get('comments'))[-MAX_THREAD_COMMENTS:],
    })
    known_pii = {'aliases': [], 'names': [n for n in [claim.client_name] if n]}
    try:
        result = AIClient.complete(
            system_prompt=SUMMARY_PROMPT,
            trusted={'claim_facts': str(facts)},
            untrusted=untrusted,
            known_pii=known_pii,
            response_schema=BriefingSummary,
            call_site='claim_summary',
            temperature=SUMMARY_TEMPERATURE,
            max_tokens=SUMMARY_MAX_TOKENS,
        )
    except Exception as e:
        logger.warning("Claim summary generation failed for claim #%s: %s", claim.id, e)
        return None
    summary = (result.summary or '').strip()
    if not summary:
        logger.warning("Claim summary came back empty for claim #%s", claim.id)
        return None
    return summary


def refresh_claim_summary(claim: 'Claim', ticket_data: Dict[str, Any]) -> bool:
    """Regenerate and store the claim's summary. True on success."""
    summary = generate_claim_summary(claim, ticket_data)
    if summary is None:
        return False
    claim.ai_summary = summary
    claim.ai_summary_updated_at = timezone.now()
    claim.save(update_fields=['ai_summary', 'ai_summary_updated_at', 'updated_at'])
    return True
