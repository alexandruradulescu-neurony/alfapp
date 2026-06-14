"""
Frontend views for LORA dashboard.
Template-based views using Bootstrap 5.
"""

import filetype
import logging
import os
import tempfile
from functools import wraps

# python-magic is optional - falls back to filetype library if libmagic is not installed
try:
    import magic
    HAS_LIBMAGIC = True
except (ImportError, OSError):
    HAS_LIBMAGIC = False
    magic = None

from django.core.cache import cache
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models
from django.db.models import Count, F, Q
from django.utils import timezone
from datetime import timedelta
from django.utils.text import get_valid_filename
from django.views.generic import CreateView, UpdateView
from django.urls import reverse_lazy
from django.views.decorators.csrf import csrf_protect

from apps.users.decorators import login_redirect, agent_required, manager_required
from django.db import transaction
from apps.claims.models import Claim, ClaimEvidence
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.users.models import User

logger = logging.getLogger(__name__)


def rate_limit_logins(max_attempts=5, timeout=60):
    """
    Decorator to rate limit login attempts.
    
    Args:
        max_attempts: Maximum number of attempts allowed
        timeout: Time window in seconds (default: 60 seconds = 1 minute)
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if request.method == 'POST':
                # Get client IP address
                ip = request.META.get('REMOTE_ADDR', '')
                cache_key = f'login_attempts_{ip}'
                
                # Get current attempts
                attempts = cache.get(cache_key, 0)
                
                if attempts >= max_attempts:
                    logger.warning(f"Rate limit exceeded for IP: {ip}")
                    from django.http import HttpResponseForbidden
                    return HttpResponseForbidden(
                        'Too many login attempts. Please try again later.',
                        content_type='text/plain'
                    )
                
                # Increment attempts
                cache.set(cache_key, attempts + 1, timeout)
            
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


# ============== Authentication Views ==============

@login_redirect
@rate_limit_logins(max_attempts=5, timeout=60)
@csrf_protect  # Explicitly enforce CSRF protection
def login_view(request):
    """Login view with role-based redirect."""
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)

            # Redirect based on role
            if user.role == 'MANAGER':
                return redirect('manager_dashboard')
            else:
                return redirect('agent_dashboard')
        else:
            messages.error(request, 'Invalid username or password.')

    from django.conf import settings as django_settings
    return render(request, 'login.html', {'debug': django_settings.DEBUG})


def logout_view(request):
    """Logout view."""
    logout(request)
    return redirect('login')


# ============== Dashboard Views ==============

def dashboard_redirect(request):
    """Redirect to appropriate dashboard based on user role."""
    if not request.user.is_authenticated:
        return redirect('login')
    
    if request.user.role == 'MANAGER':
        return redirect('manager_dashboard')
    return redirect('agent_dashboard')


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
    _ACTIVE_DISPUTE_STATUSES = ['RECEIVED', 'MATCHED', 'GATHERING_DATA', 'DOCUMENTS_READY', 'UNDER_REVIEW', 'EVIDENCE_SENT']
    disputed = Claim.objects.filter(
        disputes__status__in=_ACTIVE_DISPUTE_STATUSES
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
    """Agent claim list view.
    
    Agents see:
    - Claims assigned to them
    - Unassigned claims (available for assignment)
    
    Managers see all claims.
    
    Includes pagination to prevent loading large datasets.
    """
    user = request.user
    
    # Base queryset with annotations (optimized to prevent N+1)
    claims = Claim.objects.annotate(
        evidence_count=Count('evidence'),
        email_count=Count('emails')
    ).select_related('assigned_to').order_by('-created_at')
    
    # Filter by assigned user for agents (not managers)
    if user.role == 'AGENT':
        claims = claims.filter(
            models.Q(assigned_to=user) | models.Q(assigned_to__isnull=True)
        )
    
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
    
    # Pagination (20 claims per page)
    from django.core.paginator import Paginator
    paginator = Paginator(claims, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'page_obj': page_obj,
        'claims': page_obj,  # For template compatibility
        'status_filter': status_filter,
        'search_query': search_query,
        'status_choices': [
            (s, s) for s in Claim.objects.exclude(status='')
            .values_list('status', flat=True).distinct().order_by('status')
        ],
    }

    return render(request, 'agent/claims.html', context)


def _annotate_deadline(claim, now):
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
        elif days < 0:
            claim.deadline_state = 'overdue'
            claim.deadline_label = f'{-days}d overdue'
        elif days == 0:
            claim.deadline_state = 'soon'
            claim.deadline_label = 'due today'
        elif days <= 7:
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
    if request.user.role == 'AGENT' and claim.assigned_to and claim.assigned_to != request.user:
        messages.error(request, 'You are not assigned to this claim.')
        return redirect('agent_claims')
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
    if request.user.role == 'AGENT' and claim.assigned_to and claim.assigned_to != request.user:
        messages.error(request, 'You are not assigned to this claim.')
        return redirect('agent_claims')
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


@agent_required
def agent_claim_detail(request, claim_id):
    """Agent claim detail view."""
    claim = get_object_or_404(
        Claim.objects.prefetch_related('evidence', 'emails', 'refunds', 'disputes').select_related('assigned_to'),
        id=claim_id,
    )
    _annotate_deadline(claim, timezone.now())

    # Check if agent has permission to view this claim
    if request.user.role == 'AGENT':
        if claim.assigned_to and claim.assigned_to != request.user:
            messages.error(request, 'You are not assigned to this claim.')
            return redirect('agent_claims')

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

    context = {
        'claim': claim,
        'zd_subdomain': zd_subdomain,
        'claim_refund_status': claim.refund_status,
        'emails_open': emails_open,
        'emails_handled': emails_handled,
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
                agent = User.objects.get(id=agent_id, role='AGENT')
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

    # Agents can only upload evidence for claims assigned to them (or unassigned)
    if request.user.role == 'AGENT':
        if claim.assigned_to and claim.assigned_to != request.user:
            messages.error(request, 'You are not assigned to this claim.')
            return redirect('agent_claims')

    if request.method == 'POST':
        image = request.FILES.get('image')
        description = request.POST.get('description', '')

        if image:
            # Validate file size FIRST (max 10MB) - before reading content
            max_size = 10 * 1024 * 1024  # 10MB
            if image.size > max_size:
                messages.error(request, f'File size must be less than {max_size // 1024 // 1024}MB.')
            else:
                # Validate file extension (first line of defense)
                allowed_extensions = ['jpg', 'jpeg', 'png', 'gif', 'webp']
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
                    allowed_mime_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']

                    # Validate using python-magic for accurate MIME type detection (if available)
                    if HAS_LIBMAGIC and magic:
                        # Read first 1024 bytes for magic number detection
                        image_file = image.read(1024)
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
                    
                    # Validate file content using filetype as secondary check
                    try:
                        with tempfile.NamedTemporaryFile(delete=False) as tmp:
                            for chunk in image.chunks():
                                tmp.write(chunk)
                            tmp_path = tmp.name

                        # Use filetype to detect actual file type (secondary validation)
                        kind = filetype.guess(tmp_path)

                        if kind is None or kind.mime not in allowed_mime_types:
                            os.unlink(tmp_path)
                            messages.error(
                                request,
                                f'Invalid file content. File does not appear to be a valid image.'
                            )
                            return redirect('agent_claim_detail', claim_id=claim_id)

                        # File is valid, clean up temp file and save
                        os.unlink(tmp_path)

                        # Sanitize filename to prevent path traversal
                        image.name = get_valid_filename(image.name)

                        ClaimEvidence.objects.create(
                            claim=claim,
                            image=image,
                            description=description
                        )
                        messages.success(request, 'Evidence uploaded successfully.')

                    except Exception as e:
                        logger.error(f"Error processing file upload: {e}")
                        messages.error(request, 'Error processing file. Please try again.')
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

    # Pagination (20 emails per page)
    paginator = Paginator(emails, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Get system settings for Zendesk links (cached)
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
    agents_count = User.objects.filter(role='AGENT').count()

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
        received=Count(Case(When(status='RECEIVED', then=1), output_field=IntegerField())),
        gathering_data=Count(Case(When(status='GATHERING_DATA', then=1), output_field=IntegerField())),
        documents_ready=Count(Case(When(status='DOCUMENTS_READY', then=1), output_field=IntegerField())),
        under_review=Count(Case(When(status='UNDER_REVIEW', then=1), output_field=IntegerField())),
        evidence_sent=Count(Case(When(status='EVIDENCE_SENT', then=1), output_field=IntegerField())),
        resolved=Count(Case(When(status__in=['RESOLVED_WON', 'RESOLVED_LOST', 'ACCEPTED'], then=1), output_field=IntegerField())),
    )
    # Disputes with a response deadline in the next 3 days (or already past),
    # still open — the highest-stakes "act now" number. Missing the deadline
    # auto-loses the dispute.
    _open_dispute = ~models.Q(status__in=['RESOLVED_WON', 'RESOLVED_LOST', 'ACCEPTED'])
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

    paginator = Paginator(claims, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    # Pre-compute display state — templates shouldn't do date math
    for claim in page_obj:
        _annotate_deadline(claim, now)
        claim.days_in_status = (
            (now - claim.status_changed_at).days if claim.status_changed_at else None)
        claim.stuck = (claim.days_in_status is not None and claim.days_in_status > 14
                       and claim.status_category not in ('solved',))

    zd_subdomain = SystemSettings.get_instance().zd_subdomain
    context = {
        'page_obj': page_obj,
        'claims': page_obj,
        'stats': stats,
        'family_filter': family_filter,
        'status_filter': status_filter,
        'search_query': search_query,
        'zd_ticket_base': (f'https://{zd_subdomain}.zendesk.com/agent/tickets/'
                           if zd_subdomain else ''),
        'status_choices': [
            (s, s) for s in Claim.objects.exclude(status='')
            .values_list('status', flat=True).distinct().order_by('status')
        ],
    }

    return render(request, 'manager/claims.html', context)


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
        'total_amount': refunds.filter(status='COMPLETED').aggregate(total=Sum('amount'))['total'] or 0,
        'pending': refunds.filter(status='PENDING').count(),
        'completed': refunds.filter(status='COMPLETED').count(),
        'failed': refunds.filter(status='FAILED').count(),
    }
    
    # Pagination
    from django.core.paginator import Paginator
    paginator = Paginator(refunds, 20)
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
        'zd_ticket_base': (f'https://{zd_subdomain}.zendesk.com/agent/tickets/'
                           if zd_subdomain else ''),
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
    for service_key in ['AI', 'IMAP', 'ZENDESK', 'PAYPAL', 'SCHEDULER', 'SCREENSHOT']:
        service_statuses[service_key], _ = ServiceStatus.objects.get_or_create(
            service=service_key,
            defaults={'status': 'disconnected', 'is_enabled': True}
        )
    
    # Sidebar doesn't have a separate service status, use AI status as proxy
    sidebar_status = service_statuses['AI']

    if request.method == 'POST':
        form = SystemSettingsForm(request.POST, instance=settings)
        if form.is_valid():
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

    context = {
        'settings': settings,
        'form': form,
        'ai_status': service_statuses['AI'],
        'imap_status': service_statuses['IMAP'],
        'zd_status': service_statuses['ZENDESK'],
        'paypal_status': service_statuses['PAYPAL'],
        'scheduler_status': service_statuses['SCHEDULER'],
        'screenshot_status': service_statuses['SCREENSHOT'],
        'sidebar_status': sidebar_status,
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
        # Create new user
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        role = request.POST.get('role')
        first_name = request.POST.get('first_name', '')
        last_name = request.POST.get('last_name', '')

        if username and password and role:
            if User.objects.filter(username=username).exists():
                messages.error(request, 'Username already exists.')
            elif role not in ['AGENT', 'MANAGER']:
                messages.error(request, 'Invalid role.')
            else:
                # Validate password strength
                try:
                    validate_password(password, user=None)
                except ValidationError as e:
                    error_messages = ' '.join(e.messages)
                    messages.error(request, f'Weak password: {error_messages}')
                    return render(request, 'manager/users.html', {'users': users, 'role_choices': User.ROLE_CHOICES})

                try:
                    with transaction.atomic():
                        User.objects.create_user(
                            username=username,
                            email=email,
                            password=password,
                            role=role,
                            first_name=first_name,
                            last_name=last_name,
                        )
                    messages.success(request, f'User {username} created successfully.')
                except Exception as e:
                    logger.error(f"Error creating user {username}: {e}")
                    messages.error(request, f'Failed to create user: {str(e)}')
        else:
            messages.error(request, 'Please fill in all required fields.')

    context = {
        'users': users,
        'role_choices': User.ROLE_CHOICES,
    }

    return render(request, 'manager/users.html', context)


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
