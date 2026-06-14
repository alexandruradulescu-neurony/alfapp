"""Client progress-update cadence (the day-2/5/11/21 follow-ups after a claim
is submitted). Hybrid firing: LORA schedules the milestones; an agent triggers
"prepare" on a due one (which reads new institution replies and drafts a
progress update) and sends it as a public Zendesk reply. Draft-for-approval.

Per-office rule (see project_multi_office_submissions): a claim goes to MANY
offices at once; a single "not found" is NOT the claim outcome, and any "found"
is the good-news signal. The drafting prompt encodes this."""

import logging
from datetime import timedelta

from django.utils import timezone

from apps.communications.models import ClientUpdate, EmailLog
from apps.communications.client_report import _known_pii_for, _first_line

logger = logging.getLogger(__name__)

# Milestone → days after the claim entered the submitted status.
DAY_OFFSETS = [('DAY_2', 2), ('DAY_5', 5), ('DAY_11', 11), ('DAY_21', 21)]

FOLLOWUP_SYSTEM_PROMPT = (
    "You are a support agent at Airport Lost & Found writing a brief, warm progress "
    "update to a client about the ongoing search for their lost item. You are given "
    "the replies received so far from the lost-and-found offices / airlines we "
    "contacted on the client's behalf.\n"
    "CRITICAL: we submit to MANY offices at once, and each reply is from ONE office "
    "only. A 'not found' from one office is NOT a verdict on the claim — it only means "
    "that office has not matched the item yet; other offices may still find it. Any "
    "'found' / match is the good-news signal — lead with it. Frame negatives as 'X has "
    "not located it yet', NEVER as 'your item was not found', and note we are still "
    "awaiting the other offices.\n"
    "NEVER promise, guarantee or imply that the item will be recovered. Keep it honest, "
    "reassuring and concise, with a greeting and a sign-off from 'The Airport Lost & "
    "Found team'. Base it ONLY on the replies and facts given — do not invent anything. "
    "Return only the message body."
)


def schedule_follow_ups(claim, anchor=None):
    """Create the four follow-up milestones for a claim (idempotent). Anchor =
    when the claim entered the submitted status; defaults to now."""
    anchor = anchor or timezone.now()
    for milestone, days in DAY_OFFSETS:
        ClientUpdate.objects.get_or_create(
            claim=claim, milestone=milestone,
            defaults={'due_at': anchor + timedelta(days=days), 'state': 'SCHEDULED'},
        )


def start_client_updates(claim) -> bool:
    """Manually begin the cadence for an existing claim that never auto-triggered
    (e.g. it was already in the submitted status before this feature existed):
    draft the initial message and schedule the follow-ups, anchored now. No-op if
    updates already exist. Returns True if it started fresh, False otherwise."""
    if (claim.follow_up_updates.exists() or getattr(claim, 'client_report_draft', '')
            or getattr(claim, 'client_report_sent_at', None)):
        return False
    from apps.communications.client_report import build_client_update_message
    claim.client_report_draft = build_client_update_message(claim, polish=False)
    claim.save(update_fields=['client_report_draft', 'updated_at'])
    schedule_follow_ups(claim, timezone.now())
    return True


def cancel_open_follow_ups(claim):
    """When a claim is solved/closed (or the item is found), stop chasing it —
    mark any not-yet-sent follow-ups as skipped."""
    return claim.follow_up_updates.filter(state__in=['SCHEDULED', 'DRAFTED']).update(
        state='SKIPPED', updated_at=timezone.now())


def due_follow_ups(claim, now=None):
    """Scheduled follow-ups whose time has come (ready for an agent to prepare)."""
    now = now or timezone.now()
    return claim.follow_up_updates.filter(state='SCHEDULED', due_at__lte=now).order_by('due_at')


def _since_anchor(claim):
    """Timestamp of the most recent update sent to the client (initial or a
    follow-up), else the claim's creation — used to gather what's NEW."""
    times = []
    if getattr(claim, 'client_report_sent_at', None):
        times.append(claim.client_report_sent_at)
    last_followup = claim.follow_up_updates.filter(state='SENT').order_by('-sent_at').first()
    if last_followup and last_followup.sent_at:
        times.append(last_followup.sent_at)
    if times:
        return max(times)
    return getattr(claim, 'created_at', None) or timezone.now() - timedelta(days=30)


def _recent_office_replies(claim):
    """Institution replies (EmailLogs) received since the last client update."""
    since = _since_anchor(claim)
    return list(EmailLog.objects.filter(claim=claim, received_at__gte=since).order_by('received_at'))


def _no_news_template(claim) -> str:
    name = (getattr(claim, 'client_name', '') or '').strip() or 'there'
    obj = _first_line(getattr(claim, 'object_description', '') or '') or 'your lost item'
    return "\n".join([
        f"Dear {name},",
        "",
        f"A quick update on the search for your {obj}: we are still actively following up with the "
        "lost-and-found offices we contacted on your behalf. We do not have new information to share "
        "just yet, but please be assured the search is ongoing and we will let you know as soon as we "
        "hear anything.",
        "",
        "If you remember any further details about your item, simply reply to this message.",
        "",
        "Kind regards,",
        "The Airport Lost & Found team",
    ])


def _draft_follow_up(claim, replies) -> tuple:
    """Return (body, has_news). With no new replies → reassuring 'still
    searching' template. With replies → AI progress update (per-office rule),
    falling back to the template on any AI failure."""
    if not replies:
        return _no_news_template(claim), False
    try:
        from apps.config.models import SystemSettings
        if not getattr(SystemSettings.get_instance(), 'ai_api_key', ''):
            return _no_news_template(claim), True
        from apps.ai.client import AIClient
        from apps.ai.schemas import EmailDraft
        name = (getattr(claim, 'client_name', '') or '').strip() or 'the client'
        obj = _first_line(getattr(claim, 'object_description', '') or '') or 'their lost item'
        reply_lines = [
            f"Office: {r.from_email or 'unknown'} | reply type: {r.get_category_display()} | "
            f"summary: {(r.ai_summary or r.subject or '').strip()[:300]}"
            for r in replies
        ]
        result = AIClient.complete(
            system_prompt=FOLLOWUP_SYSTEM_PROMPT,
            trusted={'client_name': name, 'lost_item': obj},
            untrusted={'office_replies': reply_lines},
            known_pii=_known_pii_for(claim),
            response_schema=EmailDraft,
            call_site='client_followup',
            temperature=0.4,
            max_tokens=900,
        )
        body = (result.body or '').strip()
        return (body or _no_news_template(claim)), True
    except Exception as e:
        logger.warning(f"Follow-up AI draft failed for claim #{getattr(claim, 'id', '?')}; "
                       f"using fallback: {e}")
        return _no_news_template(claim), bool(replies)


def prepare_follow_up(update, fetch_email=True):
    """Prepare a due follow-up: optionally pull fresh mail, read recent office
    replies, draft the progress update, and mark it DRAFTED for agent review."""
    claim = update.claim
    if fetch_email and getattr(claim, 'email_alias', '') and getattr(claim, 'zd_ticket_id', ''):
        try:
            from apps.communications.services import check_email_for_ticket
            check_email_for_ticket(claim.zd_ticket_id, claim, claim.email_alias)
        except Exception as e:
            logger.warning(f"Follow-up email fetch failed for claim #{claim.id}: {e}")
    body, has_news = _draft_follow_up(claim, _recent_office_replies(claim))
    update.draft_body = body
    update.has_news = has_news
    update.state = 'DRAFTED'
    update.save(update_fields=['draft_body', 'has_news', 'state', 'updated_at'])
    return update


def send_follow_up(update, body) -> bool:
    """Post the (edited) follow-up as a PUBLIC Zendesk reply and mark it SENT."""
    body = (body or '').strip()
    if not body or update.state == 'SENT' or not update.claim.zd_ticket_id:
        return False
    from apps.integrations.services import post_zendesk_comment
    if post_zendesk_comment(update.claim.zd_ticket_id, body, is_internal=False) is None:
        return False
    update.draft_body = body
    update.sent_at = timezone.now()
    update.state = 'SENT'
    update.save(update_fields=['draft_body', 'sent_at', 'state', 'updated_at'])
    return True


def skip_follow_up(update):
    update.state = 'SKIPPED'
    update.save(update_fields=['state', 'updated_at'])
    return update
