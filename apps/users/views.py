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
from django.db.models import Count, Q
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
        assigned_to=request.user, status__in=['Received', 'Searching']
    ).count()
    urgent_emails = EmailLog.objects.filter(sentiment='Urgent', action_required=True).count()
    disputed = Claim.objects.filter(status='Disputed').count()

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
        'status_choices': Claim.STATUS_CHOICES,
    }
    
    return render(request, 'agent/claims.html', context)


@agent_required
def agent_claim_detail(request, claim_id):
    """Agent claim detail view."""
    claim = get_object_or_404(
        Claim.objects.prefetch_related('evidence', 'emails').select_related('assigned_to'),
        id=claim_id,
    )
    
    # Check if agent has permission to view this claim
    if request.user.role == 'AGENT':
        if claim.assigned_to and claim.assigned_to != request.user:
            messages.error(request, 'You are not assigned to this claim.')
            return redirect('agent_claims')
    
    context = {
        'claim': claim,
        'status_choices': Claim.STATUS_CHOICES,
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
                claim.save()
                messages.success(request, f'Claim assigned to {agent.username}.')
            except User.DoesNotExist:
                messages.error(request, 'Invalid agent selected.')
        else:
            # Unassign claim
            claim.assigned_to = None
            claim.save()
            messages.success(request, 'Claim unassigned.')
    
    return redirect('manager_claims')


@agent_required
@transaction.atomic
def agent_update_status(request, claim_id):
    """Update claim status."""
    claim = get_object_or_404(Claim, id=claim_id)

    # Agents can only update claims assigned to them (or unassigned)
    if request.user.role == 'AGENT':
        if claim.assigned_to and claim.assigned_to != request.user:
            messages.error(request, 'You are not assigned to this claim.')
            return redirect('agent_claims')

    if request.method == 'POST':
        new_status = request.POST.get('status')
        
        if new_status in [choice[0] for choice in Claim.STATUS_CHOICES]:
            old_status = claim.status
            claim.status = new_status
            claim.save()
            messages.success(request, f'Status updated from {old_status} to {new_status}.')
        else:
            messages.error(request, 'Invalid status selected.')
    
    return redirect('agent_claim_detail', claim_id=claim_id)


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

                        allowed_mime_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
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
    - Sentiment (Positive, Neutral, Frustrated, Urgent)
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

    # Filter by sentiment
    sentiment_filter = request.GET.get('sentiment', '')
    if sentiment_filter:
        emails = emails.filter(sentiment=sentiment_filter)

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
        'sentiment_filter': sentiment_filter,
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
    - AI analysis (summary, sentiment, category, action_required)
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
    from django.db.models import Case, When, IntegerField

    # Get all stats in a single query using annotations
    from django.db.models import Count

    stats = Claim.objects.aggregate(
        total=Count('id'),
        received=Count(Case(When(status='Received', then=1), output_field=IntegerField())),
        searching=Count(Case(When(status='Searching', then=1), output_field=IntegerField())),
        found=Count(Case(When(status='Found', then=1), output_field=IntegerField())),
        disputed=Count(Case(When(status='Disputed', then=1), output_field=IntegerField())),
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

    # Recent activity (optimized with select_related)
    recent_claims = Claim.objects.select_related('assigned_to').order_by('-created_at')[:10]
    recent_emails = EmailLog.objects.select_related('claim').order_by('-received_at')[:10]
    recent_disputes = Dispute.objects.select_related('claim').order_by('-created_at')[:5]

    context = {
        'total_claims': stats['total'],
        'received': stats['received'],
        'searching': stats['searching'],
        'found': stats['found'],
        'disputed': stats['disputed'],
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
        'recent_claims': recent_claims,
        'recent_emails': recent_emails,
        'recent_disputes': recent_disputes,
    }

    return render(request, 'manager/dashboard.html', context)


@manager_required
def manager_claims(request):
    """Manager claim overview (same as agent but with PDF access).
    
    Includes pagination to prevent loading large datasets.
    """
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

    # Get all agents for assignment dropdown
    agents = User.objects.filter(role='AGENT').order_by('username')
    
    # Pagination (20 claims per page)
    from django.core.paginator import Paginator
    paginator = Paginator(claims, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'claims': page_obj,
        'agents': agents,
        'status_filter': status_filter,
        'search_query': search_query,
        'status_choices': Claim.STATUS_CHOICES,
    }

    return render(request, 'manager/claims.html', context)


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
            # Save non-sensitive fields from the form
            form.save()

            # Handle sensitive fields - only update if provided
            sensitive_fields = ['imap_pass', 'zd_token', 'paypal_secret', 'sidebar_secret_token', 'zd_agent_password', 'ai_api_key']
            for field_name in sensitive_fields:
                value = form.cleaned_data.get(field_name)
                if value:
                    setattr(settings, field_name, value)

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


@manager_required
def test_ai(request):
    """Test AI connection and configuration.
    
    Sends a simple test prompt to the configured AI provider
    and displays the response for debugging.
    """
    from apps.config.models import SystemSettings
    from openai import OpenAI
    
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
            client = OpenAI(
                api_key=settings_obj.ai_api_key,
                base_url=settings_obj.ai_api_base,
            )
            
            response = client.chat.completions.create(
                model=settings_obj.ai_api_model,
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": test_prompt},
                ],
                temperature=0.7,
                max_tokens=200,
            )
            
            result['success'] = True
            result['message'] = 'AI connection successful!'
            result['response'] = response.choices[0].message.content
            result['tokens_used'] = response.usage.total_tokens
        except Exception as e:
            result['message'] = f'AI connection failed: {str(e)}'
            result['error'] = str(e)
    
    return render(request, 'manager/test_ai.html', result)

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
