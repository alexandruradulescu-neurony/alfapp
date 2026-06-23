"""
Frontend views for LORA dashboard.
Template-based views using Bootstrap 5.
"""

import filetype
import logging
import os
import tempfile
from datetime import datetime
from functools import wraps
from typing import Callable

# python-magic is optional - falls back to filetype library if libmagic is not installed
try:
    import magic
    HAS_LIBMAGIC = True
except (ImportError, OSError):
    HAS_LIBMAGIC = False
    magic = None

from django.core.cache import cache
from django.http import HttpRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from apps.users.forms import StaffUserCreationForm
from django.contrib import messages
from django.core.paginator import InvalidPage, Paginator
from django.db import models
from django.db.models import Case, Count, F, IntegerField, Q, When
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import timedelta
from django.utils.text import get_valid_filename
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from apps.users.decorators import login_redirect, agent_required, manager_required
from django.db import transaction
from apps.claims.models import Claim, ClaimEvidence
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.users.models import User
from apps.core.utils import get_client_ip
from apps.users.constants import (
    LOGIN_MAX_ATTEMPTS,
    LOGIN_ATTEMPT_WINDOW_SECONDS,
    EVIDENCE_MAX_BYTES,
    EVIDENCE_ALLOWED_EXTENSIONS,
    EVIDENCE_ALLOWED_MIME_TYPES,
    MAGIC_SNIFF_BYTES,
    CLAIM_STUCK_DAYS,
    LIST_PAGE_SIZE,
    DEADLINE_OVERDUE_DAYS,
    DEADLINE_DUE_TODAY_DAYS,
    DEADLINE_SOON_DAYS,
)

logger = logging.getLogger(__name__)


def _claim_status_choices() -> list[tuple[str, str]]:
    """Distinct, non-empty Claim.status values as (value, label) pairs for the
    claim-list filter dropdowns (shared by agent_claims and manager_claims)."""
    return [
        (s, s) for s in Claim.objects.exclude(status='')
        .values_list('status', flat=True).distinct().order_by('status')
    ]


def _zendesk_ticket_base(zd_subdomain: str) -> str:
    """Base URL for linking to a Zendesk ticket ('' when no subdomain is set)."""
    return f'https://{zd_subdomain}.zendesk.com/agent/tickets/' if zd_subdomain else ''


def rate_limit_logins(max_attempts: int = LOGIN_MAX_ATTEMPTS) -> Callable:
    """
    Decorator to rate limit login attempts.

    Args:
        max_attempts: Maximum number of FAILED attempts allowed per client IP
            within LOGIN_ATTEMPT_WINDOW_SECONDS (the window is owned by the view,
            which records the failures and clears them on success).
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.method == 'POST':
                ip = get_client_ip(request)
                # Only ENFORCE the ceiling here — the view records FAILED attempts
                # and clears the counter on a successful login, so a valid login
                # from a shared office IP isn't locked out by its own attempts.
                if cache.get(f'login_attempts_{ip}', 0) >= max_attempts:
                    logger.warning(f"Rate limit exceeded for IP: {ip}")
                    from django.http import HttpResponseForbidden
                    return HttpResponseForbidden(
                        'Too many login attempts. Please try again later.',
                        content_type='text/plain'
                    )
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


# ============== Authentication Views ==============

@login_redirect
@rate_limit_logins(max_attempts=LOGIN_MAX_ATTEMPTS)
@csrf_protect  # Explicitly enforce CSRF protection
def login_view(request):
    """Login view. With the role split removed there is a single dashboard, so a
    successful login always redirects there."""
    if request.method == 'POST':
        # AuthenticationForm authenticates the credentials AND fail-closes on
        # inactive accounts / missing fields. We keep a single generic error and
        # the per-IP throttle in the view so we never reveal which field or
        # credential failed (no "account inactive" leak).
        form = AuthenticationForm(request, data=request.POST)
        cache_key = f"login_attempts_{get_client_ip(request)}"
        if form.is_valid():
            cache.delete(cache_key)  # reset the failure counter on success
            login(request, form.get_user())
            return redirect('manager_dashboard')
        else:
            # Count only FAILED attempts (the throttle decorator enforces the cap).
            cache.set(cache_key, cache.get(cache_key, 0) + 1, LOGIN_ATTEMPT_WINDOW_SECONDS)
            messages.error(request, 'Invalid username or password.')

    from django.conf import settings as django_settings
    return render(request, 'login.html', {'debug': django_settings.DEBUG})


@require_POST
def logout_view(request):
    """Log the user out. POST-only (+ CSRF token in the form) so a third-party
    page cannot force a logout via a GET request."""
    logout(request)
    return redirect('login')


# ============== Dashboard Views ==============

def dashboard_redirect(request):
    """Send users to the single dashboard."""
    if not request.user.is_authenticated:
        return redirect('login')
    return redirect('manager_dashboard')


# ============== Agent Views ==============

@agent_required
def agent_dashboard(request):
    """Agent dashboard with overview stats.
    Uses optimized aggregate queries to reduce database hits.
    """
    # Get stats
    total_claims = Claim.objects.count()
    my_claims = Claim.objects.filter(
        assigned_to=request.user
    ).exclude(status_category='solved').count()
    urgent_emails = EmailLog.objects.filter(
        action_required=True,
        category__in=['RESUBMISSION_REQUIRED', 'OBJECT_NOT_FOUND']
    ).count()
    from apps.payments.models import Dispute
    disputed = Claim.objects.filter(
        disputes__status__in=Dispute.ACTIVE_STATUSES
    ).distinct().count()

    # Consolidate email stats into single aggregate query
    email_stats = EmailLog.objects.aggregate(
        total=Count('id'),
        requiring_attention=Count(Case(
            When(action_required=True, auto_resolved=False, then=1),
            output_field=IntegerField()
        )),
        auto_resolved=Count(Case(
            When(auto_resolved=True, then=1),
            output_field=IntegerField()
        )),
    )

    # Email category breakdown (already optimized)
    email_category_stats = EmailLog.objects.values('category').annotate(
        count=Count('id')
    ).order_by('-count')

    # Recent claims
    recent_claims = Claim.objects.select_related('assigned_to')[:10]

    # Recent emails
    recent_emails = EmailLog.objects.select_related('claim').order_by('-received_at')[:10]

    context = {
        'total_claims': total_claims,
        'my_claims': my_claims,
        'urgent_emails': urgent_emails,
        'disputed': disputed,
        'total_emails': email_stats['total'],
        'emails_requiring_attention': email_stats['requiring_attention'],
        'auto_resolved_emails': email_stats['auto_resolved'],
        'email_category_stats': email_category_stats,
        'recent_claims': recent_claims,
        'recent_emails': recent_emails,
    }

    return render(request, 'agent/dashboard.html', context)


@agent_required
def agent_claims(request):
    """Claim list view.

    With the AGENT/MANAGER role split removed there is a single authenticated
    user type, so every signed-in user sees all claims (no per-agent or
    assignment-based filtering). Includes pagination to prevent loading large
    datasets.
    """
    user = request.user

    # Base queryset with annotations (optimized to prevent N+1)
    claims = Claim.objects.annotate(
        evidence_count=Count('evidence'),
        email_count=Count('emails')
    ).select_related('assigned_to').order_by('-created_at')
    
    # Filter by status if provided
    status_filter = request.GET.get('status')
    if status_filter:
        claims = claims.filter(status=status_filter)

    # Filter by risk: show only unacknowledged flagged claims
    risk_filter = request.GET.get('risk')
    if risk_filter:
        claims = claims.filter(risk_acknowledged_at__isnull=True).exclude(risk_level='none')

    # Search
    search_query = request.GET.get('search')
    if search_query:
        claims = claims.filter(
            Q(client_email__icontains=search_query) |
            Q(zd_ticket_id__icontains=search_query) |
            Q(flight_details__icontains=search_query)
        )

    # Pagination
    paginator = Paginator(claims, LIST_PAGE_SIZE)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'claims': page_obj,  # For template compatibility
        'status_filter': status_filter,
        'risk_filter': risk_filter,
        'search_query': search_query,
        'status_choices': _claim_status_choices(),
    }

    return render(request, 'agent/claims.html', context)


def _annotate_deadline(claim: Claim, now: datetime) -> Claim:
    """Attach deadline_show/state/label display fields to a claim.

    Falls back to the raw deadline_date when the computed deadline_at is
    null (claims from before the status mirror)."""
    from apps.claims.services import compute_deadline_at
    claim.deadline_show = claim.deadline_at or compute_deadline_at(
        claim.deadline_date, claim.deadline_time or '',
        claim.deadline_timezone or '')
    claim.deadline_state = ''
    claim.deadline_label = ''
    if claim.deadline_show:
        days = (claim.deadline_show - now).days
        if claim.status_category == 'solved':
            claim.deadline_state = 'done'
        elif days < DEADLINE_OVERDUE_DAYS:
            claim.deadline_state = 'overdue'
            claim.deadline_label = f'{-days}d overdue'
        elif days == DEADLINE_DUE_TODAY_DAYS:
            claim.deadline_state = 'soon'
            claim.deadline_label = 'due today'
        elif days <= DEADLINE_SOON_DAYS:
            claim.deadline_state = 'soon'
            claim.deadline_label = f'{days}d left'
        else:
            claim.deadline_state = 'ok'
            claim.deadline_label = f'{days}d left'
    return claim


@agent_required
def claim_client_report_generate(request, claim_id):
    """Regenerate the client 'what we did' update draft (with AI polish) for review."""
    claim = get_object_or_404(Claim, id=claim_id)
    if request.method != 'POST':
        return redirect('agent_claim_detail', claim_id=claim_id)
    if claim.client_report_sent_at:
        messages.warning(request, 'The client update was already sent; regeneration is disabled.')
        return redirect('agent_claim_detail', claim_id=claim_id)
    from apps.communications.client_report import build_client_update_message
    claim.client_report_draft = build_client_update_message(claim, polish=True)
    claim.save(update_fields=['client_report_draft', 'updated_at'])
    messages.success(request, 'Client update draft regenerated — review it, then send.')
    return _claim_detail_response(request, claim_id)


@agent_required
def claim_client_report_send(request, claim_id):
    """Send the (edited) client update as a PUBLIC reply on the Zendesk ticket."""
    claim = get_object_or_404(Claim, id=claim_id)
    if request.method != 'POST':
        return redirect('agent_claim_detail', claim_id=claim_id)
    if claim.client_report_sent_at:
        messages.warning(request, 'The client update was already sent for this claim.')
        return redirect('agent_claim_detail', claim_id=claim_id)
    if claim.risk_active:
        messages.error(request, 'This claim is flagged at-risk — review/acknowledge before sending.')
        return redirect('agent_claim_detail', claim_id=claim_id)
    if claim.client_report_skipped_at:
        messages.warning(request, 'The initial update was skipped — un-skip it before sending.')
        return redirect('agent_claim_detail', claim_id=claim_id)
    body = (request.POST.get('body') or '').strip()
    if not body:
        messages.error(request, 'The message is empty — nothing to send.')
        return redirect('agent_claim_detail', claim_id=claim_id)
    if not claim.zd_ticket_id:
        messages.error(request, 'This claim has no Zendesk ticket to reply on.')
        return redirect('agent_claim_detail', claim_id=claim_id)

    from apps.integrations.services import post_zendesk_comment
    result = post_zendesk_comment(claim.zd_ticket_id, body, is_internal=False)
    if result is None:
        messages.error(request, 'Could not post the reply to Zendesk — please try again.')
        return redirect('agent_claim_detail', claim_id=claim_id)

    claim.client_report_draft = body
    claim.client_report_sent_at = timezone.now()
    claim.save(update_fields=['client_report_draft', 'client_report_sent_at', 'updated_at'])
    messages.success(request, 'Client update sent as a public reply on the Zendesk ticket.')
    return _claim_detail_response(request, claim_id)


@agent_required
def claim_client_report_skip(request, claim_id):
    """Toggle 'skipped' on the initial client update. For claims that reached
    LORA late and were already updated, so the initial shouldn't be sent.
    Reversible: posting again un-skips it. No-op once the update was sent."""
    claim = get_object_or_404(Claim, id=claim_id)
    if request.method == 'POST':
        if claim.client_report_sent_at:
            messages.warning(request, 'The initial update was already sent — nothing to skip.')
        elif claim.client_report_skipped_at:
            claim.client_report_skipped_at = None
            claim.save(update_fields=['client_report_skipped_at', 'updated_at'])
            messages.success(request, 'Initial update un-skipped — you can send it again.')
        else:
            claim.client_report_skipped_at = timezone.now()
            claim.save(update_fields=['client_report_skipped_at', 'updated_at'])
            messages.success(request, 'Initial update skipped — it will not be sent.')
    return _claim_detail_response(request, claim_id)


@agent_required
def client_followup_dismiss(request, claim_id, milestone):
    """Dismiss a 'missed' cadence milestone (its date passed and nothing was
    sent) — record it as skipped so it stops flagging for attention."""
    claim = get_object_or_404(Claim, id=claim_id)
    if request.method == 'POST':
        from apps.communications.models import ClientUpdate
        from apps.communications import client_updates as cu
        anchor = claim.created_at or timezone.now()
        due = next((d for m, d in cu.cadence_plan(anchor, anchor, cu._service_length_days())
                    if m == milestone), timezone.now())
        obj, created = ClientUpdate.objects.get_or_create(
            claim=claim, milestone=milestone,
            defaults={'due_at': due, 'state': ClientUpdate.STATE_SKIPPED})
        if not created and obj.state != ClientUpdate.STATE_SENT:
            obj.state = ClientUpdate.STATE_SKIPPED
            obj.save()
        messages.success(request, f'{cu.milestone_label(milestone)} dismissed.')
    return _claim_detail_response(request, claim_id)


def _followup_and_claim(request: HttpRequest, update_id: int) -> tuple["ClientUpdate", Claim]:
    """Fetch a ClientUpdate + its claim (404 if the update doesn't exist).

    Post role-removal there is no per-agent assignment guard — all authenticated
    staff may act on any follow-up — so this always returns (update, claim)."""
    from apps.communications.models import ClientUpdate
    update = get_object_or_404(ClientUpdate, id=update_id)
    return update, update.claim


@agent_required
def client_followup_prepare(request, update_id):
    """Prepare a due follow-up: read new office replies + draft the update."""
    update, claim = _followup_and_claim(request, update_id)
    if request.method == 'POST':
        from apps.communications import client_updates as cu
        cu.prepare_follow_up(update)
        messages.success(request, f'{update.label} update drafted — review it, then send.')
    return _claim_detail_response(request, claim.id)


@agent_required
def client_followup_send(request, update_id):
    """Send the (edited) follow-up as a PUBLIC Zendesk reply."""
    update, claim = _followup_and_claim(request, update_id)
    if request.method == 'POST':
        from apps.communications import client_updates as cu
        if update.state == 'SENT':
            messages.warning(request, 'That update was already sent.')
        elif not (request.POST.get('body') or '').strip():
            messages.error(request, 'The message is empty — nothing to send.')
        elif not claim.zd_ticket_id:
            messages.error(request, 'This claim has no Zendesk ticket to reply on.')
        elif cu.send_follow_up(update, request.POST.get('body')):
            messages.success(request, f'{update.label} update sent as a public Zendesk reply.')
        else:
            messages.error(request, 'Could not post the reply to Zendesk — please try again.')
    return _claim_detail_response(request, claim.id)


@agent_required
def client_followup_skip(request, update_id):
    """Skip a follow-up (agent decides it's not worth sending)."""
    update, claim = _followup_and_claim(request, update_id)
    if request.method == 'POST':
        from apps.communications import client_updates as cu
        cu.skip_follow_up(update)
        messages.success(request, f'{update.label} update skipped.')
    return _claim_detail_response(request, claim.id)


@agent_required
def client_updates_start(request, claim_id):
    """Manually begin the client-update cadence for an existing claim that never
    auto-triggered (e.g. it was already in the submitted status)."""
    claim = get_object_or_404(Claim, id=claim_id)
    if request.method == 'POST':
        from apps.communications.client_updates import start_client_updates
        if start_client_updates(claim):
            messages.success(request, 'Client updates started — review the initial draft, then send.')
        else:
            messages.info(request, 'Client updates were already started for this claim.')
    return _claim_detail_response(request, claim.id)


def _claim_detail_context(claim_id):
    """Build the context for the single-claim screen (full page and HTMX body)."""
    claim = get_object_or_404(
        Claim.objects.prefetch_related(
            'evidence', 'emails', 'refunds', 'disputes', 'follow_up_updates'
        ).select_related('assigned_to'),
        id=claim_id,
    )
    _annotate_deadline(claim, timezone.now())

    # Get Zendesk subdomain for ticket links
    try:
        system_settings = SystemSettings.get_instance()
        zd_subdomain = system_settings.zd_subdomain
    except Exception:
        zd_subdomain = ''

    # Split emails so the log shows only what still needs attention; handled
    # ones (resolved by an agent or auto-resolved) collapse out of the way.
    all_emails = list(claim.emails.all())  # prefetched
    emails_open = [e for e in all_emails if e.action_required]
    emails_handled = [e for e in all_emails if not e.action_required]

    # Follow-up client updates (day 2/5/11/21): annotate which scheduled ones
    # are now due to prepare so the template can show a "Prepare update" button.
    now = timezone.now()
    client_followups = list(claim.follow_up_updates.all())  # prefetched, ordered by due_at
    for fu in client_followups:
        fu.is_due = (fu.state == 'SCHEDULED' and fu.due_at <= now)

    # Remaining refundable amount (what the client paid minus what's already
    # refunded) — pre-fills the Grant-refund dialog; never negative.
    remaining_refund = (claim.price_paid or 0) - (claim.refund_total or 0)
    if remaining_refund < 0:
        remaining_refund = 0

    from apps.communications.client_updates import build_cadence_status
    cadence = build_cadence_status(claim)

    return claim, {
        'claim': claim,
        'zd_subdomain': zd_subdomain,
        'claim_refund_status': claim.refund_status,
        'emails_open': emails_open,
        'emails_handled': emails_handled,
        'client_followups': client_followups,
        'remaining_refund': remaining_refund,
        'cadence': cadence,
        'form_fills': claim.form_fills.exclude(form_url='')[:10],
    }


@agent_required
def agent_claim_detail(request, claim_id):
    """Agent claim detail view (full page)."""
    _claim, context = _claim_detail_context(claim_id)
    return render(request, 'agent/claim_detail.html', context)


@agent_required
def agent_claim_detail_body(request, claim_id):
    """The claim-detail screen body as an HTMX fragment (no base shell)."""
    _claim, context = _claim_detail_context(claim_id)
    return render(request, 'agent/_claim_body.html', context)


def _claim_detail_response(request, claim_id):
    """After a form action: HTMX gets the refreshed body fragment (with any
    flash messages swapped out-of-band into the toast region); plain requests
    keep the existing full-page redirect (no-JS fallback)."""
    if request.headers.get('HX-Request'):
        _claim, context = _claim_detail_context(claim_id)
        context['htmx_fragment'] = True
        return render(request, 'agent/_claim_body.html', context)
    return redirect('agent_claim_detail', claim_id=claim_id)


@manager_required
def agent_assign_claim(request, claim_id):
    """Manager view to assign a claim to an agent."""
    claim = get_object_or_404(Claim, id=claim_id)
    
    if request.method == 'POST':
        # Validate agent_id is a valid integer
        agent_id = request.POST.get('agent_id')
        
        if agent_id:
            try:
                agent_id = int(agent_id)
            except (ValueError, TypeError):
                messages.error(request, 'Invalid agent ID format.')
                return redirect('manager_claims')
            
            try:
                agent = User.objects.get(id=agent_id)
                claim.assigned_to = agent
                claim.save(update_fields=['assigned_to', 'updated_at'])
                messages.success(request, f'Claim assigned to {agent.username}.')
            except User.DoesNotExist:
                messages.error(request, 'Invalid agent selected.')
        else:
            # Unassign claim
            claim.assigned_to = None
            claim.save(update_fields=['assigned_to', 'updated_at'])
            messages.success(request, 'Claim unassigned.')
    
    return redirect('manager_claims')


@agent_required
@transaction.atomic
def agent_upload_evidence(request, claim_id):
    """Upload evidence for a claim with comprehensive file validation."""
    claim = get_object_or_404(Claim, id=claim_id)

    if request.method == 'POST':
        image = request.FILES.get('image')
        description = request.POST.get('description', '')

        if image:
            # Validate file size FIRST (max 10MB) - before reading content
            max_size = EVIDENCE_MAX_BYTES
            if image.size > max_size:
                messages.error(request, f'File size must be less than {max_size // 1024 // 1024}MB.')
            else:
                # Validate file extension (first line of defense)
                allowed_extensions = EVIDENCE_ALLOWED_EXTENSIONS
                file_ext = image.name.split('.')[-1].lower() if '.' in image.name else ''
                if file_ext not in allowed_extensions:
                    messages.error(
                        request,
                        f'Invalid file extension. Allowed extensions: {", ".join(allowed_extensions)}.'
                    )
                else:
                    # Allowed image MIME types — used by BOTH the python-magic
                    # pass (when available) and the filetype secondary check
                    # below. Defined here, outside the HAS_LIBMAGIC branch, so it
                    # is always set even when libmagic is not installed (otherwise
                    # the filetype check raises UnboundLocalError and every upload
                    # fails on hosts without libmagic).
                    allowed_mime_types = EVIDENCE_ALLOWED_MIME_TYPES

                    # Validate using python-magic for accurate MIME type detection (if available)
                    if HAS_LIBMAGIC and magic:
                        # Read first 1024 bytes for magic number detection
                        image_file = image.read(MAGIC_SNIFF_BYTES)
                        image.seek(0)  # Reset file pointer

                        try:
                            mime = magic.from_buffer(image_file, mime=True)
                        except Exception as e:
                            logger.error(f"Error detecting file type: {e}")
                            messages.error(request, 'Could not validate file content. Please try again.')
                            return redirect('agent_claim_detail', claim_id=claim_id)

                        if mime not in allowed_mime_types:
                            messages.error(request, f'Invalid file type. Detected: {mime}')
                            return redirect('agent_claim_detail', claim_id=claim_id)
                    
                    # Validate file content using filetype as secondary check.
                    tmp_path = None
                    try:
                        with tempfile.NamedTemporaryFile(delete=False) as tmp:
                            for chunk in image.chunks():
                                tmp.write(chunk)
                            tmp_path = tmp.name

                        # Use filetype to detect the actual file type.
                        kind = filetype.guess(tmp_path)
                        if kind is None or kind.mime not in allowed_mime_types:
                            messages.error(
                                request,
                                'Invalid file content. File does not appear to be a valid image.'
                            )
                            return redirect('agent_claim_detail', claim_id=claim_id)

                        # Sanitize filename to prevent path traversal
                        image.name = get_valid_filename(image.name)
                        ClaimEvidence.objects.create(
                            claim=claim,
                            image=image,
                            description=description
                        )
                        messages.success(request, 'Evidence uploaded successfully.')

                    except Exception as e:
                        logger.error(f"Error processing file upload: {e}", exc_info=True)
                        messages.error(request, 'Error processing file. Please try again.')
                    finally:
                        # Always remove the validation temp file — including the
                        # early-return and exception paths (delete=False above).
                        if tmp_path:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
        else:
            messages.error(request, 'Please select an image file.')

    return redirect('agent_claim_detail', claim_id=claim_id)


# ============== Email Management Views ==============

@agent_required
def agent_emails(request):
    """Inbound institution-email triage, segmented by an action-first TAB/lens
    (needs_reply · object_found · not_found · resubmit · handled · all), each with
    a live count. Default = needs_reply. The AI gist (ai_summary) shows inline so
    the list is triageable without opening each email. Search: subject, from_email.
    """
    # ai_summary is shown inline now, so don't defer it; body/raw_headers stay deferred.
    base = EmailLog.objects.select_related('claim').defer('body', 'raw_headers')

    needs_reply_q = Q(action_required=True, auto_resolved=False)
    lenses = {
        'needs_reply': needs_reply_q,
        'object_found': Q(category=EmailLog.CATEGORY_OBJECT_FOUND),
        'not_found': Q(category=EmailLog.CATEGORY_OBJECT_NOT_FOUND),
        'resubmit': Q(category=EmailLog.CATEGORY_RESUBMISSION_REQUIRED),
        'handled': ~needs_reply_q,
        'all': Q(),
    }
    tab = request.GET.get('tab') or 'needs_reply'
    if tab not in lenses:
        tab = 'needs_reply'

    tab_counts = {name: base.filter(q).count() for name, q in lenses.items()}
    _tab_labels = [
        ('needs_reply', 'Needs reply'), ('object_found', 'Object found'),
        ('not_found', 'Not found'), ('resubmit', 'Resubmit'),
        ('handled', 'Handled'), ('all', 'All'),
    ]
    tabs = [{'key': k, 'label': lbl, 'count': tab_counts[k], 'active': tab == k}
            for k, lbl in _tab_labels]

    emails = base.filter(lenses[tab]).order_by('-received_at')

    search_query = request.GET.get('search', '')
    if search_query:
        emails = emails.filter(
            Q(subject__icontains=search_query) | Q(from_email__icontains=search_query))

    paginator = Paginator(emails, LIST_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))

    settings = SystemSettings.get_instance()
    context = {
        'page_obj': page_obj,
        'emails': page_obj,
        'tab': tab,
        'tabs': tabs,
        'tab_counts': tab_counts,
        'search_query': search_query,
        'settings': settings,
        'zd_ticket_base': _zendesk_ticket_base(settings.zd_subdomain),
    }

    return render(request, 'agent/emails.html', context)


@agent_required
def agent_email_detail(request, email_id):
    """Full email detail view.

    Shows:
    - Full email body
    - AI analysis (summary, category, action_required)
    - Linked Zendesk ticket (if any)
    - Linked Claim (if any)
    - Raw headers (toggle)
    """
    email = get_object_or_404(
        EmailLog.objects.select_related('claim'),
        id=email_id,
    )

    # Get system settings for Zendesk links
    settings = SystemSettings.get_instance()

    # Lead the page with the one thing a manager decides here: does this reply
    # need a response? (action_required, unless the AI already auto-handled it.)
    if email.action_required and not email.auto_resolved:
        email_state = 'needs_reply'
    elif email.auto_resolved:
        email_state = 'auto'
    else:
        email_state = 'handled'

    context = {
        'email': email,
        'settings': settings,
        'email_state': email_state,
    }

    return render(request, 'agent/email_detail.html', context)


# ============== Manager Views ==============

# --- Shared claim-queue predicates ---------------------------------------------
# A number on a dashboard card must equal the list that card opens. These two
# predicates are the single source of truth for "has this claim left the
# workqueue" and "is this claim a problem", used by BOTH manager_dashboard and
# manager_claims so the card counts and the list lenses can never drift apart.

def _claim_exited_q():
    """A claim has left the active workqueue once it is genuinely done — the
    Solved/Closed family — EXCEPT 'Refund-Denied', which sits in that family at
    Zendesk but is still worked until the ticket is truly closed. Mirrors
    Claim.has_exited and the claims-list 'exited' filter."""
    return Q(status_category__in=['solved', 'closed']) & ~Q(status__icontains='denied')


def _claim_problems_q():
    """The 'Problems' lens: an unacknowledged risk flag OR an institution email
    awaiting a human reply — and the claim is still active. Expects the queryset
    to be annotated with attention_emails (count of action-required, unresolved
    emails)."""
    flagged = Q(risk_acknowledged_at__isnull=True) & ~Q(risk_level='none')
    return (flagged | Q(attention_emails__gt=0)) & ~_claim_exited_q()


@manager_required
def manager_dashboard(request):
    """Manager dashboard with overview stats.

    Optimized: Uses single query with annotations instead of 5 separate queries.
    """
    # Get all stats in a single query using annotations
    stats = Claim.objects.aggregate(
        total=Count('id'),
        active=Count(Case(When(~Q(status_category='solved'), then=1),
                          output_field=IntegerField())),
        pending_client=Count(Case(When(status_category='pending', then=1),
                                  output_field=IntegerField())),
        solved=Count(Case(When(status_category='solved', then=1),
                          output_field=IntegerField())),
    )

    # Agents count
    agents_count = User.objects.count()

    # Email stats - consolidate into single aggregate query
    email_stats = EmailLog.objects.aggregate(
        total=Count('id'),
        requiring_attention=Count(Case(
            When(action_required=True, auto_resolved=False, then=1),
            output_field=IntegerField()
        )),
        auto_resolved=Count(Case(
            When(auto_resolved=True, then=1),
            output_field=IntegerField()
        )),
    )

    # Email category breakdown
    email_category_stats = EmailLog.objects.values('category').annotate(
        count=Count('id')
    ).order_by('-count')

    # Dispute stats
    from apps.payments.models import Dispute
    dispute_stats = Dispute.objects.aggregate(
        total=Count('id'),
        received=Count(Case(When(status=Dispute.STATUS_RECEIVED, then=1), output_field=IntegerField())),
        gathering_data=Count(Case(When(status=Dispute.STATUS_GATHERING_DATA, then=1), output_field=IntegerField())),
        documents_ready=Count(Case(When(status=Dispute.STATUS_DOCUMENTS_READY, then=1), output_field=IntegerField())),
        under_review=Count(Case(When(status=Dispute.STATUS_UNDER_REVIEW, then=1), output_field=IntegerField())),
        evidence_sent=Count(Case(When(status=Dispute.STATUS_EVIDENCE_SENT, then=1), output_field=IntegerField())),
        resolved=Count(Case(When(status__in=Dispute.TERMINAL_STATUSES, then=1), output_field=IntegerField())),
    )
    # "Disputes due soon" — the act-now number. Counts ONLY disputes still ours to
    # reply to (not in PayPal's or the buyer's hands, not terminal, not overdue)
    # whose deadline lands within the next 7 days. Reuses the disputes-list query so
    # the count is always a subset of the list the card opens (?view=action). The
    # window is whole-calendar-day based (NOT "7 days from this exact second") so a
    # deadline a few minutes either side of the cutoff doesn't flicker the number
    # with the time of day.
    from apps.payments.frontend_views import _needs_action_qs
    dispute_stats['due_soon'] = _needs_action_qs(Dispute.objects.all()).filter(
        seller_response_due__isnull=False,
        seller_response_due__date__lte=timezone.localdate() + timedelta(days=DEADLINE_SOON_DAYS),
    ).count()

    # Recent activity (optimized with select_related)
    recent_claims = Claim.objects.select_related('assigned_to').order_by('-created_at')[:10]
    recent_emails = EmailLog.objects.select_related('claim').order_by('-received_at')[:10]
    recent_disputes = Dispute.objects.select_related('claim').order_by('-created_at')[:5]

    # Action-triage counts — the dashboard leads with "what needs me now", reusing
    # the SAME query helpers as the list screens so the numbers match their queues:
    #   - claims needing attention = the claims "Problems" lens (unacknowledged risk
    #     flag OR an institution email awaiting a reply) AND still active
    #   - refunds awaiting a decision = claims marked 'Refund Requested' (refunds queue)
    _claims_attn = Claim.objects.annotate(
        attention_emails=Count('emails', distinct=True,
                               filter=Q(emails__action_required=True, emails__auto_resolved=False)))
    claims_attention = _claims_attn.filter(_claim_problems_q()).distinct().count()
    refunds_pending_decision = Claim.objects.filter(status='Refund Requested').count()

    context = {
        'claims_attention': claims_attention,
        'refunds_pending_decision': refunds_pending_decision,
        'total_claims': stats['total'],
        'active': stats['active'],
        'pending_client': stats['pending_client'],
        'solved': stats['solved'],
        'disputed': dispute_stats['total'] - dispute_stats['resolved'],
        'agents_count': agents_count,
        'total_emails': email_stats['total'],
        'auto_resolved_emails': email_stats['auto_resolved'],
        'emails_requiring_attention': email_stats['requiring_attention'],
        'email_category_stats': email_category_stats,
        'dispute_total': dispute_stats['total'],
        'dispute_received': dispute_stats['received'],
        'dispute_gathering_data': dispute_stats['gathering_data'],
        'dispute_documents_ready': dispute_stats['documents_ready'],
        'dispute_under_review': dispute_stats['under_review'],
        'dispute_evidence_sent': dispute_stats['evidence_sent'],
        'dispute_resolved': dispute_stats['resolved'],
        'dispute_due_soon': dispute_stats['due_soon'],
        'recent_claims': recent_claims,
        'recent_emails': recent_emails,
        'recent_disputes': recent_disputes,
    }
    # Trend chart (top of dashboard) — default 14 days, Orders line shown.
    # Toggles re-render just the chart fragment via HTMX (manager_dashboard_chart).
    context.update(_dashboard_chart_data(14, ['claims']))

    return render(request, 'manager/dashboard.html', context)


# Dashboard trend-chart series. Orders = claims/day; Revenue = service fees
# (Claim.price_paid) /day, bucketed by the claim's created day. Each line is
# scaled to its OWN axis (left=Orders, right=Revenue) so both trends read well
# despite very different scales.
_CHART_SERIES = {
    'claims': {'label': 'Orders', 'color': '#6366f1'},   # indigo-500
    'income': {'label': 'Revenue', 'color': '#10b981'},  # emerald-500
}
# SVG geometry (viewBox units)
_CW, _CH, _CL, _CR, _CT, _CB = 720, 240, 46, 56, 14, 28


def _dashboard_chart_data(range_days, show):
    """Multi-series LINE chart for the dashboard. `show` is the list of active
    series keys (subset of _CHART_SERIES); Orders is the default. Returns
    ready-to-draw SVG geometry (polyline points, dots, dual axes, gridlines),
    so the template does no math."""
    from django.db.models import Sum
    from django.db.models.functions import TruncDate

    if range_days not in (7, 14, 30):
        range_days = 14
    show = [s for s in show if s in _CHART_SERIES]
    if not show:
        show = ['claims']
    cur = set(show)

    today = timezone.localdate()
    start = today - timedelta(days=range_days - 1)
    rows = (Claim.objects.filter(created_at__date__gte=start)
            .annotate(d=TruncDate('created_at')).values('d')
            .annotate(claims=Count('id'), income=Sum('price_paid')))
    by_date = {r['d']: r for r in rows}

    dates = [start + timedelta(days=i) for i in range(range_days)]
    data = {'claims': [], 'income': []}
    for d in dates:
        r = by_date.get(d)
        data['claims'].append(r['claims'] if r else 0)
        data['income'].append(float(r['income']) if r and r['income'] is not None else 0.0)

    plot_r, plot_b = _CW - _CR, _CH - _CB
    plot_w, plot_h = plot_r - _CL, plot_b - _CT
    n = range_days
    xs = [_CL + (i * plot_w / (n - 1) if n > 1 else 0) for i in range(n)]
    step = 1 if range_days <= 14 else 3

    def fmt(metric, v):
        return '${:,.0f}'.format(v) if metric == 'income' else str(int(v))

    lines = []
    for key in show:
        vals = data[key]
        m = max(vals) or 1
        pts, dots = [], []
        for i, v in enumerate(vals):
            x, y = xs[i], plot_b - (v / m) * plot_h
            pts.append('{:.1f},{:.1f}'.format(x, y))
            # Visual marker only; the value tooltip now lives on the per-day hover
            # column (chart_cols) so hovering anywhere in a day reveals it.
            dots.append({'x': round(x, 1), 'y': round(y, 1)})
        lines.append({'key': key, 'label': _CHART_SERIES[key]['label'],
                      'color': _CHART_SERIES[key]['color'], 'points': ' '.join(pts),
                      'dots': dots, 'total': fmt(key, sum(vals))})

    # Per-day hover columns: an invisible full-height band that, on hover, reveals
    # a guide line + a small card listing that day's value(s). Pure CSS in the
    # template (no JS / eval) — CSP-safe. Geometry is computed here so the template
    # stays math-free, matching the rest of this builder.
    half = (plot_w / (n - 1) / 2) if n > 1 else plot_w / 2
    box_w, line_h = 104, 15
    cols = []
    for i, d in enumerate(dates):
        bx0 = max(_CL, xs[i] - half)
        bx1 = min(plot_r, xs[i] + half)
        tip = [{'text': d.strftime('%b %d'), 'fill': '#111827', 'weight': 600}]
        for key in show:
            tip.append({'text': '{} {}'.format(_CHART_SERIES[key]['label'], fmt(key, data[key][i])),
                        'fill': _CHART_SERIES[key]['color'], 'weight': 500})
        for j, t in enumerate(tip):
            t['y'] = round(8 + line_h * (j + 1), 1)
        box_x = min(max(xs[i] - box_w / 2, _CL), plot_r - box_w)
        cols.append({
            'hx': round(bx0, 1), 'hy': _CT, 'hw': round(bx1 - bx0, 1), 'hh': round(plot_b - _CT, 1),
            'gx': round(xs[i], 1), 'gy0': _CT, 'gy1': round(plot_b, 1),
            'bx': round(box_x, 1), 'bw': box_w, 'bh': 10 + line_h * len(tip),
            'tx': round(box_x + 8, 1), 'lines': tip,
        })

    grid_ys = [round(_CT, 1), round((_CT + plot_b) / 2, 1), round(plot_b, 1)]

    def axis(metric):
        m = max(data[metric]) or 0
        return [{'y': grid_ys[0], 'label': fmt(metric, m)},
                {'y': grid_ys[1], 'label': fmt(metric, m / 2)},
                {'y': grid_ys[2], 'label': fmt(metric, 0)}]

    def toggle_url(key):
        ns = (cur - {key}) if key in cur else (cur | {key})
        if not ns:
            ns = {'claims'}
        return '?range={}&show={}'.format(range_days, ','.join(sorted(ns)))

    subtitle = ' · '.join('{} {}'.format(ln['total'], ln['label'].lower()) for ln in lines)

    return {
        'chart_range': range_days,
        'chart_subtitle': subtitle,
        'chart_show_param': ','.join(sorted(cur)),
        'chart_show_claims': 'claims' in cur,
        'chart_show_income': 'income' in cur,
        'chart_w': _CW, 'chart_h': _CH,
        'chart_grid_ys': grid_ys, 'chart_grid_x0': _CL, 'chart_grid_x1': plot_r,
        'chart_lines': lines,
        'chart_cols': cols,
        'chart_left_axis': axis('claims') if 'claims' in cur else None,
        'chart_right_axis': axis('income') if 'income' in cur else None,
        'chart_left_x': _CL - 6, 'chart_right_x': plot_r + 6,
        'chart_x_labels': [{'x': round(xs[i], 1), 'label': dates[i].day,
                            'show': (i % step == 0) or (i == n - 1)} for i in range(n)],
        'chart_x_label_y': _CH - 8,
        'chart_has_data': any(max(data[k]) for k in show),
        'claims_toggle_url': toggle_url('claims'),
        'income_toggle_url': toggle_url('income'),
    }


@manager_required
def manager_dashboard_chart(request):
    """HTMX fragment for the dashboard trend chart — serves the 7/14/30-day
    range and the per-series overlay toggles (?range= & ?show=claims,income)."""
    try:
        range_days = int(request.GET.get('range', 14))
    except (TypeError, ValueError):
        range_days = 14
    show = [s.strip() for s in request.GET.get('show', 'claims').split(',') if s.strip()]
    return render(request, 'manager/partials/_dashboard_chart.html',
                  _dashboard_chart_data(range_days, show))


@manager_required
def manager_claims(request):
    """Manager claim overview — the management "one screen".

    Segmented by an action-first TAB/lens (problems · object found · refunds ·
    disputes · open · solved · all), each with a live count. Lenses are
    non-exclusive views, not folders (a disputed claim also shows under Open).
    "Problems" is the act-now list: an unacknowledged risk flag, OR an
    institution email awaiting a human reply.
    """
    from django.db.models import Exists, OuterRef
    from apps.payments.models import Refund, Dispute

    now = timezone.now()

    base = Claim.objects.annotate(
        attention_emails=Count(
            'emails', distinct=True,
            filter=Q(emails__action_required=True, emails__auto_resolved=False)),
        refund_exists=Exists(Refund.objects.filter(claim=OuterRef('pk'))),
        dispute_exists=Exists(Dispute.objects.filter(claim=OuterRef('pk'))),
    )

    # Exited vs active + the Problems predicate are shared with the dashboard (see
    # _claim_exited_q / _claim_problems_q) so the cards and these lenses agree.
    # 'Refund-Denied' is in the Solved family at Zendesk but is still worked until
    # the ticket is actually closed, so it does NOT count as exited.
    exited_q = _claim_exited_q()
    active_q = ~exited_q
    lenses = {
        # Action lenses show only still-active claims — a solved/closed ticket has
        # nothing left to act on, so it never belongs in problems/found/refunds/disputes.
        'problems': _claim_problems_q(),
        'object_found': Q(status__icontains='object found') & active_q,
        'refunds': Q(refund_exists=True) & active_q,
        'disputes': Q(dispute_exists=True) & active_q,
        'open': active_q,
        'solved': exited_q,
        'all': Q(),
    }
    tab = request.GET.get('tab') or 'problems'
    if tab not in lenses:
        tab = 'problems'

    # Live per-tab counts over the whole book (distinct: the email filter joins).
    tab_counts = {name: base.filter(q).distinct().count() for name, q in lenses.items()}

    _tab_labels = [
        ('problems', 'Problems'), ('object_found', 'Object found'),
        ('refunds', 'Refunds'), ('disputes', 'Disputes'),
        ('open', 'Open'), ('solved', 'Solved'), ('all', 'All'),
    ]
    tabs = [{'key': k, 'label': lbl, 'count': tab_counts[k], 'active': tab == k}
            for k, lbl in _tab_labels]

    claims = base.filter(lenses[tab])

    search_query = request.GET.get('search')
    if search_query:
        claims = claims.filter(
            Q(client_name__icontains=search_query) |
            Q(client_email__icontains=search_query) |
            Q(zd_ticket_id__icontains=search_query) |
            Q(flight_details__icontains=search_query) |
            Q(alf_claim_id__icontains=search_query)
        )

    # Calendar filter: claims submitted on a given day (created_at, local date).
    date_raw = request.GET.get('date')
    date_filter = parse_date(date_raw) if date_raw else None
    if date_filter:
        claims = claims.filter(created_at__date=date_filter)

    claims = claims.distinct().order_by('-created_at')

    paginator = Paginator(claims, LIST_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))
    # Windowed page numbers (… collapses long runs); falls back to the full
    # range when there are few pages.
    try:
        page_range = list(paginator.get_elided_page_range(
            page_obj.number, on_each_side=1, on_ends=1))
    except InvalidPage:
        page_range = list(paginator.page_range)

    # Quiet "drifting" hint — days in the current status (templates do no date math)
    for claim in page_obj:
        claim.days_in_status = (
            (now - claim.status_changed_at).days if claim.status_changed_at else None)
        claim.stuck = (claim.days_in_status is not None and claim.days_in_status > CLAIM_STUCK_DAYS
                       and claim.status_category != 'solved')

    zd_subdomain = SystemSettings.get_instance().zd_subdomain
    context = {
        'page_obj': page_obj,
        'page_range': page_range,
        'page_ellipsis': paginator.ELLIPSIS,
        'claims': page_obj,
        'tab': tab,
        'tabs': tabs,
        'tab_counts': tab_counts,
        'search_query': search_query,
        'date_filter': date_filter,
        'zd_ticket_base': _zendesk_ticket_base(zd_subdomain),
    }

    return render(request, 'manager/claims.html', context)


@manager_required
@require_POST
def manager_claims_import(request):
    """Bulk-import EXISTING Zendesk claims into LORA by ticket id.

    Manual backlog pull: the manager pastes Zendesk ticket ids (any separators)
    and each one is copied in from Zendesk via import_claim_from_zendesk_ticket
    — the same path the email match uses. It only copies claims that already
    exist in Zendesk; it never fabricates one, and skips tickets that aren't
    claim-form tickets. Results are summarised via flash messages.
    """
    import re
    from apps.integrations.services import import_claim_from_zendesk_ticket

    raw = request.POST.get('ticket_ids', '') or ''
    # Extract every run of digits — tolerant of commas, spaces, newlines, '#',
    # and pasted ticket URLs (which carry the id as their only number).
    ticket_ids = list(dict.fromkeys(re.findall(r'\d+', raw)))  # dedupe, keep order
    if not ticket_ids:
        messages.warning(request, 'No Zendesk ticket IDs found to import.')
        return redirect('manager_claims')
    CAP = 100
    if len(ticket_ids) > CAP:
        messages.warning(
            request, f'Received {len(ticket_ids)} IDs; importing the first {CAP} only.')
        ticket_ids = ticket_ids[:CAP]

    imported, existed, skipped = [], [], []
    for tid in ticket_ids:
        try:
            claim, created = import_claim_from_zendesk_ticket(tid)
        except Exception as e:  # one bad ticket must not abort the batch
            logger.error(f"Manual claim import failed for ticket {tid}: {e}", exc_info=True)
            skipped.append(f'{tid} (error)')
            continue
        if claim is None:
            skipped.append(f'{tid} (not a claim form ticket / unreachable)')
        elif created:
            imported.append(claim.alf_claim_id or tid)
        else:
            existed.append(claim.alf_claim_id or tid)

    if imported:
        messages.success(
            request, f"Imported {len(imported)} claim(s) from Zendesk: {', '.join(imported)}.")
    if existed:
        messages.info(
            request, f"{len(existed)} already in LORA: {', '.join(existed)}.")
    if skipped:
        messages.warning(
            request, f"{len(skipped)} skipped: {', '.join(skipped)}.")
    return redirect('manager_claims')


@manager_required
def manager_refunds(request):
    """Manager refund list view.
    
    Shows all refunds with filtering and search.
    """
    from apps.payments.models import Refund
    from django.db.models import Sum

    # Claims awaiting a refund decision — Zendesk status 'Refund Requested',
    # mirrored onto the claim. This is the manager's action queue: cases that
    # need someone to issue (or decline) the refund.
    refund_requested = list(
        Claim.objects.filter(status='Refund Requested')
        .order_by(F('deadline_at').asc(nulls_last=True), '-status_changed_at')
    )

    base = Refund.objects.select_related('claim', 'created_by').order_by('-created_at')

    # Headline stats describe the WHOLE refund landscape — they must NOT change
    # when the list below is narrowed by a tab/search. Compute them from the
    # unfiltered set in a single aggregate pass, then filter only the displayed
    # list. (total_amount = money actually settled, i.e. completed refunds.)
    stats_agg = Refund.objects.aggregate(
        total=Count('id'),
        total_amount=Sum('amount', filter=Q(status=Refund.STATUS_COMPLETED)),
        pending=Count('id', filter=Q(status=Refund.STATUS_PENDING)),
        completed=Count('id', filter=Q(status=Refund.STATUS_COMPLETED)),
        failed=Count('id', filter=Q(status=Refund.STATUS_FAILED)),
    )
    stats = {
        'total': stats_agg['total'],
        'total_amount': stats_agg['total_amount'] or 0,
        'pending': stats_agg['pending'],
        'completed': stats_agg['completed'],
        'failed': stats_agg['failed'],
    }

    # Action-first lenses (non-exclusive views over the same list), keyed by ?tab.
    # "In flight" = anything still moving through the pipeline; "Failed" is the
    # only refund row that needs the manager to act (re-issue).
    in_flight_q = Q(status__in=[
        Refund.STATUS_REQUESTED, Refund.STATUS_PENDING, Refund.STATUS_PROCESSING])
    lenses = {
        'all': Q(),
        'in_flight': in_flight_q,
        'completed': Q(status=Refund.STATUS_COMPLETED),
        'failed': Q(status=Refund.STATUS_FAILED),
    }
    tab_counts = {key: base.filter(q).count() for key, q in lenses.items()}

    tab = request.GET.get('tab', 'all')
    if tab not in lenses:
        tab = 'all'
    refunds = base.filter(lenses[tab])

    search_query = request.GET.get('search')
    if search_query:
        refunds = refunds.filter(
            Q(claim__client_email__icontains=search_query) |
            Q(claim__client_name__icontains=search_query) |
            Q(claim__alf_claim_id__icontains=search_query) |
            Q(paypal_refund_id__icontains=search_query) |
            Q(reason__icontains=search_query)
        )

    tabs = [
        {'key': 'all', 'label': 'All', 'count': tab_counts['all'], 'active': tab == 'all'},
        {'key': 'in_flight', 'label': 'In flight', 'count': tab_counts['in_flight'], 'active': tab == 'in_flight'},
        {'key': 'completed', 'label': 'Completed', 'count': tab_counts['completed'], 'active': tab == 'completed'},
        {'key': 'failed', 'label': 'Failed', 'count': tab_counts['failed'], 'active': tab == 'failed'},
    ]

    # Pagination
    paginator = Paginator(refunds, LIST_PAGE_SIZE)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    zd_subdomain = SystemSettings.get_instance().zd_subdomain
    context = {
        'page_obj': page_obj,
        'refunds': page_obj,
        'refund_requested': refund_requested,
        'tab': tab,
        'tabs': tabs,
        'tab_counts': tab_counts,
        'search_query': search_query,
        'stats': stats,
        'zd_ticket_base': _zendesk_ticket_base(zd_subdomain),
    }

    return render(request, 'manager/refunds.html', context)


@manager_required
def manager_settings(request):
    """Manager system settings view with form validation."""
    from apps.config.forms import SystemSettingsForm
    from apps.config.models import ServiceStatus

    settings = SystemSettings.get_instance()

    # Get or create service statuses
    service_statuses = {}
    for service_key in ['AI', 'IMAP', 'ZENDESK', 'PAYPAL', 'SCHEDULER']:
        service_statuses[service_key], _ = ServiceStatus.objects.get_or_create(
            service=service_key,
            defaults={'status': 'disconnected', 'is_enabled': True}
        )
    
    if request.method == 'POST':
        form = SystemSettingsForm(request.POST, instance=settings)
        if form.is_valid():
            # Non-sensitive fields and the sensitive-field overrides are two writes;
            # wrap them so a failure on the second can't leave settings half-saved.
            with transaction.atomic():
                # Save non-sensitive fields
                form.save()

                # Handle sensitive fields separately - only update if new value provided
                sensitive_fields = SystemSettingsForm.SENSITIVE_FIELDS
                for field_name in sensitive_fields:
                    new_value = request.POST.get(field_name, '').strip()
                    if new_value:
                        setattr(settings, field_name, new_value)

                # Terms & Conditions PDF — only replace when a new file is
                # uploaded; an empty field leaves the existing document in place.
                tc = request.FILES.get('terms_conditions_pdf')
                if tc:
                    settings.terms_conditions_pdf = tc

                settings.save()
            messages.success(request, 'Settings saved successfully.')
        else:
            # Show form errors
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
    else:
        form = SystemSettingsForm(instance=settings)

    # Best-effort: list the Zendesk custom statuses so the manager can pick the
    # trigger status ID instead of guessing it. Never block the page on this.
    custom_statuses = []
    try:
        from apps.integrations.services import _fetch_custom_statuses
        from django.core.cache import cache
        from apps.integrations.services import CUSTOM_STATUS_CACHE_KEY, CUSTOM_STATUS_CACHE_TTL
        mapping = cache.get(CUSTOM_STATUS_CACHE_KEY)
        if mapping is None:
            # Best-effort, short timeout — this picker must never hang the page.
            mapping = _fetch_custom_statuses(timeout=4)
            cache.set(CUSTOM_STATUS_CACHE_KEY, mapping, CUSTOM_STATUS_CACHE_TTL)
        custom_statuses = sorted(
            ({'id': sid, 'name': v.get('name', ''), 'category': v.get('category', '')}
             for sid, v in mapping.items()),
            key=lambda s: s['name'].lower())
    except Exception:
        custom_statuses = []

    context = {
        'settings': settings,
        'form': form,
        'custom_statuses': custom_statuses,
        'ai_status': service_statuses['AI'],
        'imap_status': service_statuses['IMAP'],
        'zd_status': service_statuses['ZENDESK'],
        'paypal_status': service_statuses['PAYPAL'],
        'scheduler_status': service_statuses['SCHEDULER'],
    }

    return render(request, 'manager/settings.html', context)


@manager_required
def manager_form_playbooks(request):
    """Per-site form-fill playbooks: list, create, edit, enable/disable, delete.
    Instructions saved here are injected into the Browser Use brief at fill time."""
    from django.db import IntegrityError
    from apps.integrations.models import FormPlaybook

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        pid = request.POST.get('id')
        if action == 'delete' and pid:
            FormPlaybook.objects.filter(pk=pid).delete()
            messages.success(request, 'Playbook deleted.')
            return redirect('manager_form_playbooks')
        domain = (request.POST.get('domain') or '').strip().lower()
        if not domain:
            messages.error(request, 'A form domain is required (e.g. chargerback.com).')
            return redirect('manager_form_playbooks')

        if action == 'suggest':
            # Draft improved instructions from recent runs; show the draft in the textarea
            # WITHOUT saving — the user reviews and clicks Save to apply.
            from apps.integrations.playbooks import (
                recent_run_summaries, suggest_playbook_instructions)
            summaries = recent_run_summaries(domain)
            if not summaries:
                messages.info(request, f'No recorded runs for {domain} yet — run a fill on this form first.')
                return redirect('manager_form_playbooks')
            draft = suggest_playbook_instructions(request.POST.get('instructions') or '', summaries)
            if not draft:
                messages.error(request, 'Could not draft instructions just now (AI unavailable). Try again.')
                return redirect('manager_form_playbooks')
            messages.success(request, f'Draft ready below from {len(summaries)} recent run(s) — '
                                      f'review it and click Save to apply.')
            playbooks = list(FormPlaybook.objects.all())
            for pb in playbooks:
                if pb.domain == domain:
                    pb.instructions = draft
                    pb.has_draft = True
            return render(request, 'manager/form_playbooks.html',
                          {'playbooks': playbooks, 'active': 'manager_form_playbooks'})

        pb = get_object_or_404(FormPlaybook, pk=pid) if pid else FormPlaybook()
        pb.domain = domain
        pb.label = (request.POST.get('label') or '').strip()
        pb.instructions = request.POST.get('instructions') or ''
        pb.enabled = request.POST.get('enabled') == 'on'
        try:
            pb.save()
            messages.success(request, 'Playbook saved.')
        except IntegrityError:
            messages.error(request, f'A playbook for "{domain}" already exists — edit that one.')
        return redirect('manager_form_playbooks')

    return render(request, 'manager/form_playbooks.html',
                  {'playbooks': FormPlaybook.objects.all(), 'active': 'manager_form_playbooks'})


@manager_required
def manager_users(request):
    """Staff user management. Creation goes through StaffUserCreationForm, which
    brings password confirmation (password1/password2) and Django's full password
    validation against the prospective user — including the similarity-to-username
    check the previous validate_password(user=None) call could not perform."""
    users = User.objects.order_by('-date_joined')

    if request.method == 'POST':
        form = StaffUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f'User {user.username} created successfully.')
        else:
            # Surface the form's field errors (username taken, password too weak,
            # passwords don't match, too similar to username, missing fields).
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)

    return render(request, 'manager/users.html', {'users': users})


@agent_required
def claim_acknowledge_risk(request, claim_id):
    """Acknowledge a claim's risk flag (clears the active badge). POST-only."""
    claim = get_object_or_404(Claim, id=claim_id)
    if request.method != 'POST':
        return redirect('agent_claim_detail', claim_id=claim_id)
    if claim.risk_active:
        claim.acknowledge_risk(request.user)
        messages.success(request, 'Risk flag acknowledged.')
    return _claim_detail_response(request, claim_id)


@manager_required
def test_ai(request):
    """Test AI connection and configuration.

    Sends a simple test prompt to the configured AI provider
    and displays the response for debugging.
    """
    from apps.config.models import SystemSettings
    from apps.ai.client import AIClient
    from apps.ai.schemas import ChatAnswer
    from apps.ai.exceptions import AIResponseValidationError, AIClientError

    settings_obj = SystemSettings.get_instance()
    result = {
        'success': False,
        'message': '',
        'response': '',
        'config': {
            'provider': settings_obj.ai_provider,
            'api_base': settings_obj.ai_api_base,
            'api_model': settings_obj.ai_api_model,
            'api_key_configured': bool(settings_obj.ai_api_key),
        }
    }

    # Check if API key is configured
    if not settings_obj.ai_api_key:
        result['message'] = 'AI API Key is not configured. Please add your API key in System Settings.'
        return render(request, 'manager/test_ai.html', result)

    if request.method == 'POST':
        test_prompt = request.POST.get('test_prompt', 'Say hello')

        try:
            ai_result = AIClient.complete(
                system_prompt=(
                    "You are a helpful assistant for AI connectivity testing. "
                    'Reply briefly in JSON like {"answer": "...", "sources": []}.'
                ),
                trusted={'manager_prompt': test_prompt},
                untrusted={},
                response_schema=ChatAnswer,
                call_site="ai_diagnostic",
            )
            result['success'] = True
            result['message'] = 'AI connection successful!'
            result['response'] = ai_result.answer
        except (AIResponseValidationError, AIClientError) as e:
            result['message'] = f'AI connection failed: {str(e)}'
            result['error'] = str(e)

    return render(request, 'manager/test_ai.html', result)
