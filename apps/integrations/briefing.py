"""Shared AI business context + the claim summary engine.

The stored claim summary (claim.ai_summary) is written ONLY here — by the
Zendesk webhook (creation + status change) and the "Refresh from Zendesk"
view. The sidebar briefing endpoint shares the business context but stays
read-only (no stored-summary writes from agent clicks). All AI calls go
through apps/ai/AIClient (PII tokenization — never a passthrough)."""
import logging
import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from django.utils import timezone

from apps.ai.client import AIClient
from apps.ai.schemas import BriefingSummary
from apps.integrations.services import build_claim_facts, build_ticket_thread

if TYPE_CHECKING:
    from apps.claims.models import Claim

logger = logging.getLogger(__name__)

_RISK_KEYWORDS = [
    (re.compile(r'\bscam\b', re.I), 'hostile_language'),
    (re.compile(r'\bfrauds?\b', re.I), 'hostile_language'),
    (re.compile(r'charge\s?backs?', re.I), 'dispute_risk'),
    (re.compile(r'\b(lawyer|attorney)s?\b', re.I), 'dispute_risk'),
    (re.compile(r'\bBBB\b'), 'dispute_risk'),
]
# Deliberately excludes 'refund'/'dispute'/'complaint' — this business names a
# NON-REFUNDABLE fee on every claim and 'dispute' is routine PayPal vocabulary,
# so those would flag nearly every case. Refund-demand detection is left to the AI.
_HARD_REASONS = {'refund_demanded', 'dispute_risk', 'hostile_language', 'status_regression'}


def keyword_risk_reasons(text: str) -> set:
    found = set()
    for rx, reason in _RISK_KEYWORDS:
        if rx.search(text or ''):
            found.add(reason)
    return found


def merge_risk(*, ai_level: str, ai_reasons, ai_note: str, thread_text: str):
    """Combine the AI risk read with the keyword booster. at_risk requires an
    AI-corroborated hard reason (or AI level at_risk); a keyword-only hard reason
    caps at 'watch' (it may be a quote, e.g. \"not a scam\")."""
    ai_reasons = set(ai_reasons or [])
    kw_reasons = keyword_risk_reasons(thread_text)
    reasons = sorted(ai_reasons | kw_reasons)
    if ai_level == 'at_risk' or (ai_reasons & _HARD_REASONS):
        level = 'at_risk'
    elif reasons:
        level = 'watch'
    else:
        level = 'none'
    return level, reasons, (ai_note or '').strip()


def _thread_text(ticket_data) -> str:
    parts = [ticket_data.get('subject', ''), ticket_data.get('description', '')]
    for c in (ticket_data.get('comments') or []):
        if isinstance(c, dict):
            parts.append(c.get('body', '') or c.get('text', ''))
        elif isinstance(c, str):
            parts.append(c)
    return '\n'.join(p for p in parts if p)


# Most recent ticket comments fed into the claim-summary AI context.
MAX_THREAD_COMMENTS = 30

# AI-tuning defaults for the claim-summary call (kept named so summary behaviour
# is tweaked in one place).
SUMMARY_TEMPERATURE = 0.4
SUMMARY_MAX_TOKENS = 4096

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
    "Every claim originates from an abandoned online checkout/cart — that is simply how a "
    "ticket enters this system, so EVERY claim begins this way. It is the universal starting "
    "point and carries NO information about the case; never mention 'abandoned', the abandoned "
    "cart, or the checkout origin in any summary, delta, analysis, or note. "
    "ALF has no staff at airports and cannot search physically — it works by reporting, "
    "calling and emailing. Comments marked 'internal note' are ALF staff notes; 'public' "
    "ones are visible to the client. Comments are listed in chronological order. Person "
    "names and contact details may appear as <NAME_...>/<EMAIL_...>/<PHONE_...> "
    "placeholders — treat each placeholder as that person or value and repeat it "
    "verbatim when referring to them, INCLUDING the angle brackets "
    "(write <NAME_12ab34cd>, never NAME_12ab34cd). "
) + STATUS_VOCABULARY

SUMMARY_PROMPT = ALF_BUSINESS_CONTEXT + (
    "\n\nWrite a concise management summary of this claim's current state in `summary`.\n"
    "LEAD with what is BLOCKING progress and needs a human, if anything. Above all, CLIENT "
    "RESPONSIVENESS: if updates were sent but the client has not replied, or the client is "
    "unreachable, say that FIRST — the search is stalled pending the client. Then the settled "
    "facts: the search focus, and which lost-and-found offices were contacted with their "
    "per-office status (no response from one office is NOT an outcome). When a check was re-run "
    "(e.g. flight verification), TRUST THE MOST RECENT verdict — do NOT present a superseded "
    "earlier check as an unresolved conflict or narrate the back-and-forth. Note the item's "
    "value/sensitivity and the abandoned-cart origin + fee paid when relevant. Keep it to what "
    "helps decide the NEXT ACTION; omit intermediate AI reasoning.\n"
    "Also assess CLIENT risk (the paying customer, not the lost-and-found institutions):\n"
    "- risk_reasons: any of ['hostile_language','refund_demanded','dispute_risk',"
    "'negative_sentiment'] that the CLIENT exhibits. Use 'refund_demanded' only when the "
    "client asks for their money BACK — NOT when they merely agreed to the non-refundable fee. "
    "Use 'dispute_risk' for threats of a chargeback/PayPal dispute/legal action/BBB.\n"
    "- risk_level: 'at_risk' if any of those reasons is clearly present, else 'watch' for mild "
    "dissatisfaction, else 'none'.\n"
    "- risk_note: one short sentence naming the signal, or '' if none.\n"
    "\n\nThe status vocabulary above explains what each status NAME means — use it only to "
    "interpret the current label. ALWAYS defer to the actual claim facts and ticket thread for "
    "what has happened; do NOT assert process steps (e.g. whether loss reports were filed) from "
    "the status name if the thread shows otherwise.\n"
    "Also produce `delta`: 1-2 sentences on what is NEW since the previous update note provided "
    "in the context. If nothing material has changed beyond any status transition, respond with "
    "exactly 'No new information.'.\n"
    'Respond as JSON: {"summary": "...", "delta": "...", "risk_level": "...", '
    '"risk_reasons": [...], "risk_note": "..."}.'
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


def generate_claim_summary(claim: 'Claim', ticket_data: dict, previous_note: str = '') -> Optional['BriefingSummary']:
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
            trusted={'claim_facts': str(facts), 'previous_update_note': previous_note or '(none)'},
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
    return result


def refresh_claim_summary(claim: 'Claim', ticket_data: Dict[str, Any], previous_note: str = '') -> Optional[str]:
    """Regenerate and store the claim's summary. Returns the delta string on
    success (never empty — falls back to 'No new information.'), or None on failure."""
    result = generate_claim_summary(claim, ticket_data, previous_note=previous_note)
    if result is None:
        return None
    claim.ai_summary = result.summary.strip()
    claim.ai_summary_updated_at = timezone.now()
    claim.save(update_fields=['ai_summary', 'ai_summary_updated_at', 'updated_at'])
    level, reasons, note = merge_risk(
        ai_level=result.risk_level, ai_reasons=result.risk_reasons,
        ai_note=result.risk_note, thread_text=_thread_text(ticket_data))
    claim.register_risk(reasons=reasons, level=level, detail=note)
    return (result.delta or '').strip() or 'No new information.'
