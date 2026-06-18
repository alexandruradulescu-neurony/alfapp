"""Client progress-update cadence (the day-2/5/11/21 follow-ups after a claim
is submitted, then a +10-day tail, then a final end-of-service email).

CASCADE scheduling: at any time exactly ONE update is "open" (scheduled or
drafted). When it is sent or skipped we create the NEXT one — so a claim that
is cancelled, refunded, found, or closed simply never gets its next link, no
cleanup needed. The cadence length is driven by the configurable service
length (SystemSettings.service_length_days); all the raw day numbers live in
apps.communications.constants.

Two firing modes share these primitives:
  - Hybrid/manual (default): an agent clicks "prepare" on a due update, reviews
    the draft, and sends it. Every send is agent-approved.
  - Autonomous (SystemSettings.client_updates_autosend ON): the
    run_client_updates command drafts AND sends due updates itself.

Per-office rule (see project_multi_office_submissions): a claim goes to MANY
offices at once; a single "not found" is NOT the claim outcome, and any "found"
is the good-news signal. The drafting prompt encodes this. Object-found is
ALWAYS handled manually by an agent — the autonomous runner never auto-sends a
"found"."""

import logging
from datetime import timedelta

from django.utils import timezone

from apps.communications.models import ClientUpdate, EmailLog
from apps.communications.client_report import _known_pii_for, _first_line
from apps.communications.constants import (
    DEFAULT_SERVICE_LENGTH_DAYS,
    FINAL_MILESTONE,
    SINCE_ANCHOR_FALLBACK_DAYS,
    cadence_offsets,
    CLIENT_UPDATE_TAG_PREFIX,
    ATTENTION_TAGS,
    FINAL_TERMINAL_TAGS,
)
from apps.integrations.services import (
    add_zendesk_ticket_tags,
    get_zendesk_ticket_tags,
    remove_zendesk_ticket_tags,
)

logger = logging.getLogger(__name__)

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
    "Do NOT relay administrative or harmful-to-the-client notices from offices "
    "(e.g. 'your submission expired', 'case closed on our side', internal reference "
    "chatter). Only relay information that is helpful or reassuring to the client; "
    "if an office message is purely negative housekeeping, omit it and simply note "
    "the search continues with the other offices.\n"
    "NEVER promise, guarantee or imply that the item will be recovered. Keep it honest, "
    "reassuring and concise, with a greeting and a sign-off from 'The Airport Lost & "
    "Found team'. Base it ONLY on the replies and facts given — do not invent anything. "
    "Return only the message body."
)

# DETERMINISTIC safety gate: only these institution-reply categories are ever
# fed to the drafting model, so harmful/administrative housekeeping can never be
# relayed to the client — not even by the autonomous runner, which posts with no
# human review. A single office's "not found" (OBJECT_NOT_FOUND), a "submission
# expired / case closed" notice (RESUBMISSION_REQUIRED), and uncategorised mail
# (UNKNOWN) are dropped. New/unknown categories default to NOT client-safe. The
# soft prompt instruction above is belt-and-suspenders; THIS allowlist is the
# real guard. (EmailLog.CATEGORY_CHOICES lives in apps/communications/models.py.)
# OBJECT_FOUND stays in the allowlist for drafting, but the runner always HOLDS
# object-found for a human (it is never auto-sent).
CLIENT_SAFE_REPLY_CATEGORIES = {
    EmailLog.CATEGORY_OBJECT_FOUND,
    EmailLog.CATEGORY_SUBMISSION_CONFIRMATION,
    EmailLog.CATEGORY_GENERAL_CORRESPONDENCE,
}


# --- View-facing API (action routing + timeline payload) ---------------------

def apply_update_action(claim, *, action, kind, body, update_id) -> str:
    """Route a client-update action (start / prepare / send / skip) across the
    initial message and the follow-up cadence, applying the guards and returning
    the human-readable result string. Takes already-parsed params (no DRF
    dependency); delegates the work to the cadence primitives in this module."""
    from apps.communications import client_updates as cu

    if action == 'start':
        return ('Client updates started — the initial draft is ready and follow-ups scheduled.'
                if cu.start_client_updates(claim) else 'Updates already started for this claim.')

    if kind == 'initial':
        if action == 'prepare':
            cu.regenerate_initial_update(claim)
            return 'Initial update regenerated.'
        if action == 'send':
            if claim.client_report_sent_at:
                return 'The initial update was already sent.'
            if not body or not claim.zd_ticket_id:
                return 'Nothing to send.'
            return ('Initial update sent as a public reply.'
                    if cu.send_initial_update(claim, body)
                    else 'Could not post the reply to Zendesk.')
        return ''

    # follow-up
    update = claim.follow_up_updates.filter(id=update_id).first()
    if not update:
        return 'Update not found.'
    if action == 'prepare':
        cu.prepare_follow_up(update)
        return f'{update.label} update drafted.'
    if action == 'skip':
        cu.skip_follow_up(update)
        return f'{update.label} update skipped.'
    if action == 'send':
        if update.state == 'SENT':
            return 'That update was already sent.'
        if cu.send_follow_up(update, body):
            return f'{update.label} update sent as a public reply.'
        return 'Could not post the reply to Zendesk.'
    return ''


def build_client_update_timeline(claim) -> dict:
    """Build the sidebar timeline payload: the initial update (if drafted/sent)
    followed by the scheduled/drafted/sent follow-ups, in due order."""
    from django.utils import timezone
    now = timezone.now()
    items = []
    if claim.client_report_draft or claim.client_report_sent_at:
        items.append({
            'kind': 'initial', 'label': 'Initial update', 'due_label': 'On submission',
            'state': 'sent' if claim.client_report_sent_at else 'drafted',
            'body': claim.client_report_draft,
            'has_news': True,
            'sent_at': claim.client_report_sent_at.isoformat() if claim.client_report_sent_at else None,
            'can_send': bool(claim.zd_ticket_id),
        })
    for fu in claim.follow_up_updates.all().order_by('due_at'):
        items.append({
            'kind': 'followup', 'id': fu.id, 'label': fu.label,
            'milestone': fu.milestone, 'state': fu.state.lower(),
            'due_at': fu.due_at.isoformat(),
            'is_due': fu.state == 'SCHEDULED' and fu.due_at <= now,
            'has_news': fu.has_news, 'body': fu.draft_body,
            'sent_at': fu.sent_at.isoformat() if fu.sent_at else None,
            'can_send': bool(claim.zd_ticket_id),
        })
    return {'claim': True, 'alf_id': claim.alf_claim_id or '', 'items': items}


# --- Service length ----------------------------------------------------------

def _service_length_days() -> int:
    """Configured length of the service in days (falls back to the constant)."""
    try:
        from apps.config.models import SystemSettings
        v = SystemSettings.get_instance().service_length_days
        return int(v) if v and int(v) > 0 else DEFAULT_SERVICE_LENGTH_DAYS
    except Exception:
        return DEFAULT_SERVICE_LENGTH_DAYS


# --- Tag ledger --------------------------------------------------------------

def tag_for_milestone(claim, milestone) -> str:
    """Return the Zendesk ledger tag for a given milestone on this claim.

    The tag is `client_update_{ordinal}` where ordinal is the milestone's
    1-based position in the cadence plan (DAY_2 → 1, DAY_5 → 2, …, FINAL →
    last). Returns '' if the milestone is not in the plan (unknown key).

    The ordinal is computed from the current service length so it shifts
    correctly when the plan is extended: e.g. if service_length_days is 32
    a DAY_31 tail step is inserted before FINAL, making FINAL ordinal 6 instead
    of 5. This ensures the manual-macro tag sequence always matches LORA's."""
    svc = _service_length_days()
    ordered = [f'DAY_{d}' for d in cadence_offsets(svc)] + [FINAL_MILESTONE]
    try:
        idx = ordered.index(milestone)
        return f'{CLIENT_UPDATE_TAG_PREFIX}{idx + 1}'
    except ValueError:
        return ''


# --- Cadence plan ------------------------------------------------------------

def _offset_for(milestone) -> int | None:
    """Days-after-submission encoded in a 'DAY_<n>' milestone key (None for FINAL)."""
    if milestone and milestone.startswith('DAY_'):
        try:
            return int(milestone[4:])
        except ValueError:
            return None
    return None


def cadence_plan(submission_anchor, creation_anchor, service_length_days):
    """The full ordered schedule for one claim: every progress update inside the
    service window (anchored to SUBMISSION) followed by the single end-of-service
    FINAL email (anchored to CREATION). Returns [(milestone_key, due_at), …].

    The cadence is anchored to submission but the FINAL to creation, and in the
    real workflow submission lags creation by days — so for some service lengths
    the raw FINAL date can fall BEFORE the last cadence update, which would send
    a contradictory "service ended" note right after a "still searching" one. We
    clamp the FINAL to always land at least a day after the last cadence due."""
    plan = [
        (f'DAY_{d}', submission_anchor + timedelta(days=d))
        for d in cadence_offsets(service_length_days)
    ]
    final_due = creation_anchor + timedelta(days=service_length_days)
    if plan and final_due <= plan[-1][1]:
        final_due = plan[-1][1] + timedelta(days=1)
    plan.append((FINAL_MILESTONE, final_due))
    return plan


def _submission_anchor(claim, fallback):
    """Stable submission moment for a claim's cadence. Derived from the earliest
    existing cadence row (its due_at minus its offset) so every milestone is
    computed from the same instant; falls back to the given value on first use."""
    first = (claim.follow_up_updates
             .exclude(milestone=FINAL_MILESTONE).order_by('due_at').first())
    if first:
        off = _offset_for(first.milestone)
        if off is not None:
            return first.due_at - timedelta(days=off)
    return fallback


def schedule_next(claim, submission_anchor=None, skip_past=False):
    """Cascade step: ensure the NEXT update exists. No-op if one is already open
    (scheduled or drafted) — we only advance once the current one is resolved.
    Returns the newly-created ClientUpdate, or None if nothing was created.

    skip_past=True (used when starting the cadence late on an already-aged claim)
    advances past any milestone whose due moment has already passed, so we jump to
    the next FUTURE milestone instead of queuing a long-overdue early one. The
    ongoing cadence leaves it False, so a merely-overdue milestone is still
    scheduled (and shown as due) rather than skipped."""
    if claim.follow_up_updates.filter(state__in=ClientUpdate.OPEN_STATES).exists():
        return None
    sub_anchor = _submission_anchor(claim, submission_anchor or timezone.now())
    creation_anchor = getattr(claim, 'created_at', None) or sub_anchor
    plan = cadence_plan(sub_anchor, creation_anchor, _service_length_days())
    existing = set(claim.follow_up_updates.values_list('milestone', flat=True))
    # Forward-only: advance to the milestone right AFTER the furthest one already
    # reached, so we never back-fill an earlier step (e.g. if only FINAL exists).
    next_index = 0
    for i, (milestone, _due) in enumerate(plan):
        if milestone in existing:
            next_index = i + 1
    if skip_past:
        now = timezone.now()
        while next_index < len(plan) and plan[next_index][1] <= now:
            next_index += 1
    if next_index >= len(plan):
        return None
    milestone, due_at = plan[next_index]
    obj, _ = ClientUpdate.objects.get_or_create(
        claim=claim, milestone=milestone,
        defaults={'due_at': due_at, 'state': ClientUpdate.STATE_SCHEDULED},
    )
    return obj


def start_client_updates(claim) -> bool:
    """Manually begin the cadence for an existing claim that never auto-triggered
    (e.g. it was already in the submitted status before this feature existed):
    draft the initial message and schedule the first follow-up still in the
    future. The cadence is anchored to the claim's submission (created_at), so
    starting late skips milestones whose date already passed instead of queuing
    a stale "Day 2". No-op if updates already exist. Returns True if it started
    fresh."""
    if (claim.follow_up_updates.exists() or getattr(claim, 'client_report_draft', '')
            or getattr(claim, 'client_report_sent_at', None)):
        return False
    from apps.communications.client_report import build_client_update_message
    from django.db import transaction
    claim.client_report_draft = build_client_update_message(claim, polish=False)
    # Draft + first schedule are one unit: a crash between them would leave a
    # drafted report with no scheduled follow-up cadence.
    anchor = getattr(claim, 'created_at', None) or timezone.now()
    with transaction.atomic():
        claim.save(update_fields=['client_report_draft', 'updated_at'])
        schedule_next(claim, anchor, skip_past=True)
    return True


def sync_cadence_for_status(claim, custom_status_id) -> None:
    """Drive the client-update cadence off a Zendesk status change. Called by the
    status-mirror webhook AFTER claim.status has been set to the new status, so it
    compares claim.status (reproducing the old in-view compare against new_status).

    When the claim first enters the configured submitted-status, draft the initial
    "what we did" message (template-only — fast) and schedule the first follow-up.
    Always, if the claim is now closed, stop the cadence. Side effects only; the
    caller owns the broad try/except that makes this best-effort."""
    from apps.config.models import SystemSettings
    ss = SystemSettings.get_instance()
    trigger_id = (ss.client_report_trigger_status_id or '').strip()
    trigger_name = (ss.client_report_trigger_status or '').strip()
    entered_trigger = (
        (trigger_id and str(custom_status_id) == trigger_id)
        or (not trigger_id and trigger_name and claim.status == trigger_name)
    )
    if (entered_trigger
            and claim.client_report_sent_at is None and not claim.client_report_draft):
        from apps.communications.client_report import build_client_update_message
        claim.client_report_draft = build_client_update_message(claim, polish=False)
        claim.save(update_fields=['client_report_draft', 'updated_at'])
        schedule_next(claim, timezone.now())
        logger.info("Client update drafted + first follow-up scheduled for claim #%s", claim.id)
    # Stop the cadence when the claim is voided — solved, an open dispute, or an
    # actual refund (claim_is_closed covers all three).
    if claim_is_closed(claim):
        cancel_open_follow_ups(claim)


def regenerate_initial_update(claim) -> bool:
    """Redraw the initial client update with POLISHED wording (the sidebar
    Regenerate button). Always returns True. NB: polish=True here is deliberately
    different from the template-only (polish=False) draft used by the auto-cadence
    and start_client_updates — do not unify them."""
    from apps.communications.client_report import build_client_update_message
    claim.client_report_draft = build_client_update_message(claim, polish=True)
    claim.save(update_fields=['client_report_draft', 'updated_at'])
    return True


def send_initial_update(claim, body) -> bool:
    """Post the initial client update as a PUBLIC Zendesk reply and record it as
    sent. Mirrors send_follow_up: returns False (writing no state) if the post
    fails, True after recording sent. The post happens BEFORE the state save (same
    accepted-risk ordering as send_follow_up). The caller owns the already-sent and
    empty-body/no-ticket guards and all user-facing strings; `body` is the edited
    body to send, saved verbatim (not regenerated).

    PAUSED while claim.risk_active: returns False without posting or writing state."""
    if getattr(claim, 'risk_active', False):
        logger.info("send_initial_update blocked for at-risk claim #%s", getattr(claim, 'id', '?'))
        return False
    from apps.integrations.services import post_zendesk_comment
    if post_zendesk_comment(claim.zd_ticket_id, body, is_internal=False) is None:
        return False
    claim.client_report_draft = body
    claim.client_report_sent_at = timezone.now()
    claim.save(update_fields=['client_report_draft', 'client_report_sent_at', 'updated_at'])
    return True


def cancel_open_follow_ups(claim):
    """When a claim is solved/closed (or the item is found), stop chasing it —
    skip any not-yet-sent updates. Does NOT advance the cascade."""
    return claim.follow_up_updates.filter(state__in=ClientUpdate.OPEN_STATES).update(
        state=ClientUpdate.STATE_SKIPPED, updated_at=timezone.now())


def due_follow_ups(claim, now=None):
    """Scheduled updates for ONE claim whose time has come (ready to prepare)."""
    now = now or timezone.now()
    return claim.follow_up_updates.filter(
        state=ClientUpdate.STATE_SCHEDULED, due_at__lte=now).order_by('due_at')


def due_updates(now=None):
    """Every scheduled update across ALL claims whose time has come — the work
    queue for the autonomous runner."""
    now = now or timezone.now()
    return (ClientUpdate.objects.filter(state=ClientUpdate.STATE_SCHEDULED, due_at__lte=now)
            .select_related('claim').order_by('due_at'))


def object_found(claim) -> bool:
    """True if any office has reported the item found. This is the good-news
    signal: an agent calls the client and handles it manually, and the final
    end-of-service email is never sent."""
    return EmailLog.objects.filter(claim=claim, category=EmailLog.CATEGORY_OBJECT_FOUND).exists()


def claim_is_closed(claim) -> bool:
    """Void/stop signal: stop messaging the client when the claim is solved/
    closed, has an OPEN dispute, or has actually been refunded. (A merely
    requested/pending/failed refund must NOT void the cadence — only a completed
    one — and an open dispute must, even while the ticket is still 'open'.)"""
    if (getattr(claim, 'status_category', '') or '') == 'solved':
        return True
    try:
        from apps.payments.models import Refund
        if claim.refunds.filter(status=Refund.STATUS_COMPLETED).exists():
            return True
    except Exception:
        pass
    try:
        from apps.payments.models import Dispute
        if claim.disputes.exclude(status__in=Dispute.TERMINAL_STATUSES).exists():
            return True
    except Exception:
        pass
    return False


# --- Drafting ----------------------------------------------------------------

def _since_anchor(claim):
    """Timestamp of the most recent update sent to the client (initial or a
    follow-up), else the claim's creation — used to gather what's NEW."""
    times = []
    if getattr(claim, 'client_report_sent_at', None):
        times.append(claim.client_report_sent_at)
    last_followup = claim.follow_up_updates.filter(
        state=ClientUpdate.STATE_SENT).order_by('-sent_at').first()
    if last_followup and last_followup.sent_at:
        times.append(last_followup.sent_at)
    if times:
        return max(times)
    return getattr(claim, 'created_at', None) or timezone.now() - timedelta(
        days=SINCE_ANCHOR_FALLBACK_DAYS)


def _recent_office_replies(claim):
    """Institution replies (EmailLogs) received since the last client update."""
    since = _since_anchor(claim)
    return list(EmailLog.objects.filter(claim=claim, received_at__gte=since).order_by('received_at'))


def _draft_follow_up(claim, replies, ss=None, *, fallback_body: str = '') -> tuple:
    """Return (body, has_news). Only CLIENT-SAFE office replies are ever shown to
    the model (CLIENT_SAFE_REPLY_CATEGORIES) — administrative/harmful housekeeping
    is dropped here, deterministically, BEFORE drafting, so it can never reach the
    client even on the unattended autonomous path. With no client-safe news →
    the on-brand per-milestone fallback body (has_news False); otherwise → AI
    progress update (per-office rule), falling back to `fallback_body` on any AI
    failure.

    `fallback_body` is the milestone_message(...) string pre-computed by the
    caller (prepare_follow_up) so this function never needs to re-fetch ticket
    data.  Pass `ss` (a SystemSettings instance) to reuse one already-loaded
    singleton — the autonomous runner threads it down so the cadence loop reads
    it once instead of re-querying get_instance() for every due update."""
    safe = [r for r in replies if r.category in CLIENT_SAFE_REPLY_CATEGORIES]
    if not safe:
        return fallback_body, False
    try:
        from apps.config.models import SystemSettings
        if ss is None:
            ss = SystemSettings.get_instance()
        if not getattr(ss, 'ai_api_key', ''):
            return fallback_body, True
        from apps.ai.client import AIClient
        from apps.ai.schemas import EmailDraft
        name = (getattr(claim, 'client_name', '') or '').strip() or 'the client'
        obj = _first_line(getattr(claim, 'object_description', '') or '') or 'their lost item'
        reply_lines = [
            f"Office: {r.from_email or 'unknown'} | reply type: {r.get_category_display()} | "
            f"summary: {(r.ai_summary or r.subject or '').strip()[:300]}"
            for r in safe
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
        return (body or fallback_body), True
    except Exception as e:
        logger.warning("Follow-up AI draft failed for claim #%s; using fallback: %s",
                       getattr(claim, 'id', '?'), e)
        return fallback_body, bool(safe)


def prepare_follow_up(update, fetch_email=True, ss=None):
    """Prepare a due update: optionally pull fresh mail, then draft it and mark
    it DRAFTED for review. The FINAL milestone uses the end-of-service template;
    every other milestone drafts a progress update from recent office replies,
    falling back to the per-milestone macro-voice template when there is no news.

    `ss` (an optional SystemSettings instance) is threaded down to the drafter so
    the autonomous runner can load the singleton once for the whole queue."""
    from apps.integrations.services import fetch_zendesk_ticket
    from apps.communications.client_update_templates import milestone_message

    claim = update.claim
    if fetch_email and getattr(claim, 'email_alias', '') and getattr(claim, 'zd_ticket_id', ''):
        try:
            from apps.communications.services import check_email_for_ticket
            check_email_for_ticket(claim.zd_ticket_id, claim, claim.email_alias)
        except Exception as e:
            logger.warning("Follow-up email fetch failed for claim #%s: %s", claim.id, e)

    # Fetch live ticket data once so milestone_message can resolve placeholders.
    ticket_data = None
    if getattr(claim, 'zd_ticket_id', ''):
        try:
            ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
        except Exception as e:
            logger.warning("Ticket fetch for milestone template failed (claim #%s): %s",
                           getattr(claim, 'id', '?'), e)

    period_days = _service_length_days()

    if update.milestone == FINAL_MILESTONE:
        body = milestone_message(claim, 'FINAL', ticket_data, period_days)
        has_news = False
    else:
        # Pre-compute the on-brand fallback once so _draft_follow_up can use it
        # as the fallback for: no safe replies, no API key, or AI failure.
        # ticket_data and period_days were fetched above — no double-fetch needed.
        fallback = milestone_message(claim, update.milestone, ticket_data, period_days)
        replies = _recent_office_replies(claim)
        safe = [r for r in replies if r.category in CLIENT_SAFE_REPLY_CATEGORIES]
        if safe:
            # AI-drafted path (office replies present); falls back to on-brand
            # milestone voice on AI failure or missing key.
            body, has_news = _draft_follow_up(claim, replies, ss=ss, fallback_body=fallback)
        else:
            # No-news path: use the on-brand per-milestone template directly.
            body = fallback
            has_news = False

    update.draft_body = body
    update.has_news = has_news
    update.state = ClientUpdate.STATE_DRAFTED
    update.save(update_fields=['draft_body', 'has_news', 'state', 'updated_at'])
    return update


def send_follow_up(update, body) -> bool:
    """Post the (edited) update as a PUBLIC Zendesk reply, mark it SENT, and
    cascade-schedule the next milestone.

    PAUSED while update.claim.risk_active: returns False without posting or writing state."""
    body = (body or '').strip()
    if getattr(update.claim, 'risk_active', False):
        logger.info("send_follow_up blocked for at-risk claim #%s", getattr(update.claim, 'id', '?'))
        return False
    if not body or update.state == ClientUpdate.STATE_SENT or not update.claim.zd_ticket_id:
        return False
    from apps.integrations.services import post_zendesk_comment
    if post_zendesk_comment(update.claim.zd_ticket_id, body, is_internal=False) is None:
        return False
    # ACCEPTED RISK: we post the public reply first, then record SENT. If the post
    # succeeds but this save() raises, a later run can re-post the same reply
    # (double-send). We keep this order deliberately — the alternative (mark SENT
    # first) risks silently dropping a reply if the post then fails, which is
    # worse for the client. The _claim_due_update CAS + the in-memory SENT guard
    # above already collapse the common races; a save() failure right here is rare
    # and a duplicate "we're still searching" note is the tolerable failure mode.
    update.draft_body = body
    update.sent_at = timezone.now()
    update.state = ClientUpdate.STATE_SENT
    update.save(update_fields=['draft_body', 'sent_at', 'state', 'updated_at'])
    # WRITE-after: stamp the ledger tag so the manual and automated paths stay in
    # sync. Tag failures are intentionally swallowed (helpers never raise) — a
    # tagging hiccup must not roll back a successfully-delivered client message.
    claim = update.claim
    tag = tag_for_milestone(claim, update.milestone)
    if tag:
        add_zendesk_ticket_tags(claim.zd_ticket_id, [tag])
    remove_zendesk_ticket_tags(claim.zd_ticket_id, list(ATTENTION_TAGS))
    if update.milestone == FINAL_MILESTONE:
        add_zendesk_ticket_tags(claim.zd_ticket_id, list(FINAL_TERMINAL_TAGS))
    schedule_next(claim)
    return True


def skip_follow_up(update):
    """Skip a single update but keep the cadence going (schedule the next one)."""
    update.state = ClientUpdate.STATE_SKIPPED
    update.save(update_fields=['state', 'updated_at'])
    schedule_next(update.claim)
    return update


# --- Autonomous runner -------------------------------------------------------

def _claim_due_update(update_id, now):
    """Atomically take ownership of a still-scheduled update so two overlapping
    runs (or a runner racing an agent) can't both send it: flip SCHEDULED→DRAFTED
    in a single UPDATE and only proceed if THIS call made the change. Returns the
    refreshed update, or None if someone else already claimed it."""
    claimed = ClientUpdate.objects.filter(
        pk=update_id, state=ClientUpdate.STATE_SCHEDULED).update(
        state=ClientUpdate.STATE_DRAFTED, updated_at=now)
    if not claimed:
        return None
    return ClientUpdate.objects.select_related('claim').get(pk=update_id)


def run_due_updates(now=None) -> dict:
    """Process every due update when autosend is ON: draft it, and send it as a
    public Zendesk reply unless it must stay manual. Idempotent and safe to run
    on any cadence (even overlapping). Returns a small summary dict.

    Rules:
      - claim solved / open-dispute / refunded → cancel the whole cadence (no send).
      - object found → leave the update DRAFTED for an agent (who calls the
        client); never auto-send a 'found'. The FINAL is also held, never silently
        suppressed, so a stray 'possible match' can't deny a genuinely-not-found
        client their end-of-service note.
      - FINAL → send only if the item was never found.
      - send failed (Zendesk down) → revert to SCHEDULED so the next run retries,
        instead of freezing the claim's cadence on a stuck draft.
      - otherwise → draft and send, then cascade to the next milestone.
    """
    from apps.config.models import SystemSettings
    now = now or timezone.now()
    # Read the settings singleton ONCE for the whole queue and thread it into the
    # drafter, rather than re-querying get_instance() for every due update.
    ss = SystemSettings.get_instance()
    if not getattr(ss, 'client_updates_autosend', False):
        return {'enabled': False, 'sent': 0, 'held': 0, 'skipped': 0, 'failed': 0,
                'considered': 0, 'already_tagged': 0}

    sent = held = skipped = failed = considered = already_tagged = 0
    for due in list(due_updates(now)):
        considered += 1
        update = _claim_due_update(due.pk, now)
        if update is None:
            continue  # already claimed by a concurrent run or an agent
        claim = update.claim
        if claim_is_closed(claim):
            cancel_open_follow_ups(claim)
            skipped += 1
            continue
        if claim.risk_active:
            # Claim flagged at-risk (hostile client / refund demanded / scam /
            # status regression, not yet acknowledged). Leave the update SCHEDULED
            # so it is retried automatically once the risk is acknowledged.
            logger.info("Claim #%s: at-risk — holding %s update for a human.",
                        claim.id, update.milestone)
            update.state = ClientUpdate.STATE_SCHEDULED
            update.save(update_fields=['state', 'updated_at'])
            held += 1
            continue
        # READ-before: if an agent already sent this update manually (the macro
        # stamps the same client_update_N tag), treat it as done — advance the
        # cascade without posting a duplicate.
        existing_tags = get_zendesk_ticket_tags(claim.zd_ticket_id)
        due_tag = tag_for_milestone(claim, update.milestone)
        if due_tag and due_tag in existing_tags:
            logger.info("Claim #%s: milestone %s already tagged (%s) — skipping duplicate send.",
                        claim.id, update.milestone, due_tag)
            update.state = ClientUpdate.STATE_SKIPPED
            update.save(update_fields=['state', 'updated_at'])
            schedule_next(claim)
            already_tagged += 1
            continue
        prepare_follow_up(update, ss=ss)  # fetches fresh mail + drafts (FINAL uses its template)
        found = object_found(claim)
        if found:
            # Good news (or an ambiguous 'possible match') — a human handles this:
            # they call the client, and decide whether the FINAL note still fits.
            # Leave it DRAFTED; the cascade pauses on the open update.
            logger.info("Claim #%s: object reported found — holding %s update for an "
                        "agent instead of auto-sending.", claim.id, update.milestone)
            held += 1
            continue
        if send_follow_up(update, update.draft_body):
            sent += 1
        else:
            # Transient Zendesk failure — don't strand the cadence on a stuck
            # draft; put it back so the next run retries.
            update.state = ClientUpdate.STATE_SCHEDULED
            update.save(update_fields=['state', 'updated_at'])
            failed += 1
    return {'enabled': True, 'sent': sent, 'held': held, 'skipped': skipped,
            'failed': failed, 'considered': considered, 'already_tagged': already_tagged}
