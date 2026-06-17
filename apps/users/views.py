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
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models
from django.db.models import Count, F, Q
from django.utils import timezone
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
        username = request.POST.get('username')
        password = request.POST.get('password')

        user = authenticate(request, username=username, password=password)

        cache_key = f"login_attempts_{get_client_ip(request)}"
        if user is not None:
            cache.delete(cache_key)  # reset the failure counter on success
            login(request, user)
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
    from django.db.models import Case, When, IntegerField
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
    recent_claims = Claim.objects.select_related('assigned_to').prefetch_related('evidence')[:10]

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
    
    # Search
    search_query = request.GET.get('search')
    if search_query:
        claims = claims.filter(
            Q(client_email__icontains=search_query) |
            Q(zd_ticket_id__icontains=search_query) |
            Q(flight_details__icontains=search_query)
        )
    
    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(claims, LIST_PAGE_SIZE)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'claims': page_obj,  # For template compatibility
        'status_filter': status_filter,
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
    return redirect('agent_claim_detail', claim_id=claim_id)


@agent_required
def claim_client_report_send(request, claim_id):
    """Send the (edited) client update as a PUBLIC reply on the Zendesk ticket."""
    claim = get_object_or_404(Claim, id=claim_id)
    if request.method != 'POST':
        return redirect('agent_claim_detail', claim_id=claim_id)
    if claim.client_report_sent_at:
        messages.warning(request, 'The client update was already sent for this claim.')
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
    return redirect('agent_claim_detail', claim_id=claim_id)


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
    return redirect('agent_claim_detail', claim_id=claim.id)


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
    return redirect('agent_claim_detail', claim_id=claim.id)


@agent_required
def client_followup_skip(request, update_id):
    """Skip a follow-up (agent decides it's not worth sending)."""
    update, claim = _followup_and_claim(request, update_id)
    if request.method == 'POST':
        from apps.communications import client_updates as cu
        cu.skip_follow_up(update)
        messages.success(request, f'{update.label} update skipped.')
    return redirect('agent_claim_detail', claim_id=claim.id)


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
    return redirect('agent_claim_detail', claim_id=claim.id)


@agent_required
def agent_claim_detail(request, claim_id):
    """Agent claim detail view."""
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

    context = {
        'claim': claim,
        'zd_subdomain': zd_subdomain,
        'claim_refund_status': claim.refund_status,
        'emails_open': emails_open,
        'emails_handled': emails_handled,
        'client_followups': client_followups,
    }

    return render(request, 'agent/claim_detail.html', context)


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
    """Email list view with filters.

    Agents can filter emails by:
    - Category (OBJECT_FOUND, OBJECT_NOT_FOUND, etc.)
    - Action Required (True/False)
    - Auto Resolved (toggle to show/hide)

    Default: Hide auto_resolved=True emails (show only emails needing attention)
    Search by: subject, from_email
    Pagination: 20 emails per page
    """
    from django.core.paginator import Paginator

    # Base queryset — defer heavy text fields not needed in list view
    emails = EmailLog.objects.select_related('claim').defer(
        'body', 'raw_headers', 'ai_summary'
    ).order_by('-received_at')

    # Default: Hide auto-resolved emails (show only emails needing attention)
    show_auto_resolved = request.GET.get('show_auto_resolved', '') == '1'
    if not show_auto_resolved:
        emails = emails.filter(auto_resolved=False)

    # Filter by category
    category_filter = request.GET.get('category', '')
    if category_filter:
        emails = emails.filter(category=category_filter)

    # Filter by action_required
    action_required_filter = request.GET.get('action_required', '')
    if action_required_filter == '1':
        emails = emails.filter(action_required=True)
    elif action_required_filter == '0':
        emails = emails.filter(action_required=False)

    # Search by subject or from_email
    search_query = request.GET.get('search', '')
    if search_query:
        emails = emails.filter(
            Q(subject__icontains=search_query) |
            Q(from_email__icontains=search_query)
        )

    # Pagination
    paginator = Paginator(emails, LIST_PAGE_SIZE)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Get system settings for Zendesk links (get_instance hits the DB each call)
    settings = SystemSettings.get_instance()

    context = {
        'page_obj': page_obj,
        'emails': page_obj,
        'search_query': search_query,
        'category_filter': category_filter,
        'action_required_filter': action_required_filter,
        'show_auto_resolved': show_auto_resolved,
        'settings': settings,
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

    context = {
        'email': email,
        'settings': settings,
    }

    return render(request, 'agent/email_detail.html', context)


# ============== Manager Views ==============

@manager_required
def manager_dashboard(request):
    """Manager dashboard with overview stats.

    Optimized: Uses single query with annotations instead of 5 separate queries.
    """
    from django.db.models import Case, When, IntegerField, Q

    # Get all stats in a single query using annotations
    from django.db.models import Count

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
    # Disputes with a response deadline in the next 3 days (or already past),
    # still open — the highest-stakes "act now" number. Missing the deadline
    # auto-loses the dispute.
    _open_dispute = ~models.Q(status__in=Dispute.TERMINAL_STATUSES)
    dispute_stats['due_soon'] = Dispute.objects.filter(
        _open_dispute, seller_response_due__isnull=False,
        seller_response_due__lte=timezone.now() + timedelta(days=3),
    ).count()

    # Recent activity (optimized with select_related)
    recent_claims = Claim.objects.select_related('assigned_to').order_by('-created_at')[:10]
    recent_emails = EmailLog.objects.select_related('claim').order_by('-received_at')[:10]
    recent_disputes = Dispute.objects.select_related('claim').order_by('-created_at')[:5]

    context = {
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

    return render(request, 'manager/dashboard.html', context)


@manager_required
def manager_claims(request):
    """Manager claim overview — the management "one screen".

    Built around what a manager scans for: who the client is, where the
    case stands (family-colored status + how long it has sat there), the
    deadline (nearest first, overdue on top), and which cases have
    institution emails waiting on a human. Solved/closed cases are hidden
    by default ('Active'); headline counters always reflect the whole book.
    """
    from django.core.paginator import Paginator

    from django.db.models.functions import Cast, Coalesce

    now = timezone.now()

    # Older claims carry only the raw deadline_date — the computed
    # deadline_at exists just for claims created/refreshed since the status
    # mirror shipped. Everything here (sorting, overdue count, display)
    # works off whichever the claim has.
    deadline_eff = Coalesce(
        'deadline_at', Cast('deadline_date', models.DateTimeField()))

    # Headline numbers are unfiltered — the state of the whole book
    active_qs = Claim.objects.exclude(status_category='solved')
    stats = {
        'active': active_qs.count(),
        'overdue': active_qs.annotate(deadline_eff=deadline_eff)
                            .filter(deadline_eff__lt=now).count(),
        'attention': Claim.objects.filter(
            emails__action_required=True, emails__auto_resolved=False,
        ).distinct().count(),
        'total': Claim.objects.count(),
    }

    claims = Claim.objects.annotate(
        email_count=Count('emails', distinct=True),
        attention_emails=Count(
            'emails', distinct=True,
            filter=Q(emails__action_required=True, emails__auto_resolved=False),
        ),
        deadline_eff=deadline_eff,
    )

    # Family quick-filter; default hides solved/closed cases
    family_filter = request.GET.get('family') or 'active'
    if family_filter == 'active':
        claims = claims.exclude(status_category='solved')
    elif family_filter in ('new', 'open', 'pending', 'hold', 'solved'):
        claims = claims.filter(status_category=family_filter)
    else:
        family_filter = 'all'

    status_filter = request.GET.get('status')
    if status_filter:
        claims = claims.filter(status=status_filter)

    search_query = request.GET.get('search')
    if search_query:
        claims = claims.filter(
            Q(client_name__icontains=search_query) |
            Q(client_email__icontains=search_query) |
            Q(zd_ticket_id__icontains=search_query) |
            Q(flight_details__icontains=search_query) |
            Q(alf_claim_id__icontains=search_query)
        )

    # Urgency order: nearest deadline first (overdue leads), undated last
    claims = claims.order_by(F('deadline_eff').asc(nulls_last=True), '-created_at')

    paginator = Paginator(claims, LIST_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))

    # Pre-compute display state — templates shouldn't do date math
    for claim in page_obj:
        _annotate_deadline(claim, now)
        claim.days_in_status = (
            (now - claim.status_changed_at).days if claim.status_changed_at else None)
        claim.stuck = (claim.days_in_status is not None and claim.days_in_status > CLAIM_STUCK_DAYS
                       and claim.status_category not in ('solved',))

    zd_subdomain = SystemSettings.get_instance().zd_subdomain
    context = {
        'page_obj': page_obj,
        'claims': page_obj,
        'stats': stats,
        'family_filter': family_filter,
        'status_filter': status_filter,
        'search_query': search_query,
        'zd_ticket_base': _zendesk_ticket_base(zd_subdomain),
        'status_choices': _claim_status_choices(),
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

    refunds = Refund.objects.select_related('claim', 'created_by').order_by('-created_at')
    
    # Filter by status
    status_filter = request.GET.get('status')
    if status_filter:
        refunds = refunds.filter(status=status_filter)
    
    # Filter by source
    source_filter = request.GET.get('source')
    if source_filter:
        refunds = refunds.filter(external_source=source_filter)
    
    # Search
    search_query = request.GET.get('search')
    if search_query:
        refunds = refunds.filter(
            Q(claim__client_email__icontains=search_query) |
            Q(paypal_refund_id__icontains=search_query) |
            Q(reason__icontains=search_query)
        )
    
    # Get statistics
    stats = {
        'total': refunds.count(),
        'total_amount': refunds.filter(status=Refund.STATUS_COMPLETED).aggregate(total=Sum('amount'))['total'] or 0,
        'pending': refunds.filter(status=Refund.STATUS_PENDING).count(),
        'completed': refunds.filter(status=Refund.STATUS_COMPLETED).count(),
        'failed': refunds.filter(status=Refund.STATUS_FAILED).count(),
    }

    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(refunds, LIST_PAGE_SIZE)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    zd_subdomain = SystemSettings.get_instance().zd_subdomain
    context = {
        'page_obj': page_obj,
        'refunds': page_obj,
        'refund_requested': refund_requested,
        'status_filter': status_filter,
        'source_filter': source_filter,
        'search_query': search_query,
        'stats': stats,
        'status_choices': Refund.STATUS_CHOICES,
        'source_choices': Refund.SOURCE_CHOICES,
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
def manager_users(request):
    """Manager user management view.

    Uses transaction.atomic() to ensure user creation is atomic.
    Includes password validation to enforce strong passwords.
    """
    from django.db import transaction
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    users = User.objects.order_by('-date_joined')

    if request.method == 'POST':
        # Create new user (single user type — no role)
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        first_name = request.POST.get('first_name', '')
        last_name = request.POST.get('last_name', '')

        if username and password:
            if User.objects.filter(username=username).exists():
                messages.error(request, 'Username already exists.')
            else:
                # Validate password strength
                try:
                    validate_password(password, user=None)
                except ValidationError as e:
                    error_messages = ' '.join(e.messages)
                    messages.error(request, f'Weak password: {error_messages}')
                    return render(request, 'manager/users.html', {'users': users})

                try:
                    with transaction.atomic():
                        User.objects.create_user(
                            username=username,
                            email=email,
                            password=password,
                            first_name=first_name,
                            last_name=last_name,
                        )
                    messages.success(request, f'User {username} created successfully.')
                except Exception as e:
                    logger.error(f"Error creating user {username}: {e}")
                    messages.error(request, f'Failed to create user: {str(e)}')
        else:
            messages.error(request, 'Please fill in all required fields.')

    return render(request, 'manager/users.html', {'users': users})


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
