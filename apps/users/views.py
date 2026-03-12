"""
Frontend views for LORA dashboard.
Template-based views using Bootstrap 5.
"""

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models
from django.db.models import Count, Q
from django.views.generic import CreateView, UpdateView
from django.urls import reverse_lazy

from apps.users.decorators import login_redirect, agent_required, manager_required
from apps.claims.models import Claim, ClaimEvidence
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.users.models import User


# ============== Authentication Views ==============

@login_redirect
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
    """Agent dashboard with overview stats."""
    # Get stats
    total_claims = Claim.objects.count()
    my_claims = Claim.objects.filter(
        assigned_to=request.user, status__in=['Received', 'Searching']
    ).count()
    urgent_emails = EmailLog.objects.filter(sentiment='Urgent', action_required=True).count()
    disputed = Claim.objects.filter(status='Disputed').count()

    # Email stats
    total_emails = EmailLog.objects.count()
    emails_requiring_attention = EmailLog.objects.filter(action_required=True, auto_resolved=False).count()
    auto_resolved_emails = EmailLog.objects.filter(auto_resolved=True).count()

    # Email category breakdown
    email_category_stats = EmailLog.objects.values('category').annotate(
        count=Count('id')
    ).order_by('-count')

    # Recent claims
    recent_claims = Claim.objects.select_related().prefetch_related('evidence')[:10]

    # Recent emails
    recent_emails = EmailLog.objects.select_related('claim').order_by('-received_at')[:10]

    context = {
        'total_claims': total_claims,
        'my_claims': my_claims,
        'urgent_emails': urgent_emails,
        'disputed': disputed,
        'total_emails': total_emails,
        'emails_requiring_attention': emails_requiring_attention,
        'auto_resolved_emails': auto_resolved_emails,
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
        agent_id = request.POST.get('agent_id')
        
        if agent_id:
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
def agent_upload_evidence(request, claim_id):
    """Upload evidence for a claim."""
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
            # Validate file size (max 10MB)
            max_size = 10 * 1024 * 1024  # 10MB
            if image.size > max_size:
                messages.error(request, f'File size must be less than {max_size // 1024 // 1024}MB.')
            else:
                # Validate file type
                allowed_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']
                if image.content_type not in allowed_types:
                    messages.error(
                        request,
                        f'Invalid file type. Allowed types: JPEG, PNG, GIF, WebP.'
                    )
                else:
                    # Validate file extension
                    allowed_extensions = ['jpg', 'jpeg', 'png', 'gif', 'webp']
                    file_ext = image.name.split('.')[-1].lower() if '.' in image.name else ''
                    if file_ext not in allowed_extensions:
                        messages.error(
                            request,
                            f'Invalid file extension. Allowed extensions: {", ".join(allowed_extensions)}.'
                        )
                    else:
                        ClaimEvidence.objects.create(
                            claim=claim,
                            image=image,
                            description=description
                        )
                        messages.success(request, 'Evidence uploaded successfully.')
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

    # Base queryset
    emails = EmailLog.objects.select_related('claim').order_by('-received_at')

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

    # Get system settings for Zendesk links
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

    # Email stats
    total_emails = EmailLog.objects.count()
    auto_resolved_emails = EmailLog.objects.filter(auto_resolved=True).count()
    emails_requiring_attention = EmailLog.objects.filter(action_required=True, auto_resolved=False).count()

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
        'total_emails': total_emails,
        'auto_resolved_emails': auto_resolved_emails,
        'emails_requiring_attention': emails_requiring_attention,
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
    """Manager system settings view."""
    settings = SystemSettings.get_instance()

    if request.method == 'POST':
        # Update non-sensitive fields
        settings.ai_prompt_template = request.POST.get('ai_prompt_template', '')
        settings.imap_host = request.POST.get('imap_host', '')
        settings.imap_user = request.POST.get('imap_user', '')
        settings.zd_subdomain = request.POST.get('zd_subdomain', '')
        settings.zd_email = request.POST.get('zd_email', '')
        settings.paypal_client_id = request.POST.get('paypal_client_id', '')
        settings.paypal_webhook_id = request.POST.get('paypal_webhook_id', '')
        
        # Update sensitive fields ONLY if new value provided (don't overwrite with empty string)
        imap_pass = request.POST.get('imap_pass', '').strip()
        if imap_pass:
            settings.imap_pass = imap_pass
        
        zd_token = request.POST.get('zd_token', '').strip()
        if zd_token:
            settings.zd_token = zd_token
        
        paypal_secret = request.POST.get('paypal_secret', '').strip()
        if paypal_secret:
            settings.paypal_secret = paypal_secret
        
        sidebar_token = request.POST.get('sidebar_secret_token', '').strip()
        if sidebar_token:
            settings.sidebar_secret_token = sidebar_token

        settings.save()
        messages.success(request, 'Settings saved successfully.')

    context = {
        'settings': settings,
    }

    return render(request, 'manager/settings.html', context)


@manager_required
def manager_users(request):
    """Manager user management view.

    Uses transaction.atomic() to ensure user creation is atomic.
    """
    from django.db import transaction
    
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
