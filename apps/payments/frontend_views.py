"""
Frontend views for Dispute Management (MANAGER role only).

Provides UI views for managing PayPal disputes:
- List disputes with filters
- View dispute details
- Generate/edit/accept/delete documents
- Send evidence to PayPal
- Accept claims
- Capture screenshots
"""

import logging
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.db.models import Q, Count
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST

from apps.users.decorators import manager_required
from apps.payments.models import Dispute, DisputeDocument, DisputeScreenshot, DisputeActivityLog
from apps.payments.document_service import generate_response_letter, generate_evidence_report
from apps.payments.paypal_disputes_service import provide_evidence, accept_claim
from apps.payments.screenshot_service import capture_screenshots_manual

logger = logging.getLogger(__name__)

# Allowlist for rendering AI-generated document HTML. The AI's output is shown
# with |safe in the preview, so strip anything executable (scripts, event
# handlers, styles) while keeping document formatting.
_SAFE_HTML_TAGS = ['p', 'br', 'b', 'strong', 'i', 'em', 'u', 'ul', 'ol', 'li',
                   'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody',
                   'tr', 'td', 'th', 'span', 'div', 'a', 'blockquote', 'hr', 'pre']
_SAFE_HTML_ATTRS = {'a': ['href', 'title'], '*': ['class']}


def sanitize_document_html(html: str) -> str:
    """Strip executable content from AI-generated HTML before it is rendered
    with |safe (defense against prompt-injection producing <script>/onerror)."""
    import bleach
    return bleach.clean(html or '', tags=_SAFE_HTML_TAGS, attributes=_SAFE_HTML_ATTRS,
                        strip=True)


@manager_required
def dispute_list(request):
    """
    List all disputes with filtering and pagination.

    GET /manager/disputes/

    Filters:
    - status: Filter by dispute status
    - search: Search by ticket ID or buyer email

    Template: manager/disputes.html
    """
    # Get filter parameters
    status_filter = request.GET.get('status', '')
    search_query = request.GET.get('search', '')

    # Start with all disputes
    disputes = Dispute.objects.all()

    # Apply status filter
    if status_filter:
        disputes = disputes.filter(status=status_filter)

    # Apply search filter (ticket ID or email)
    if search_query:
        disputes = disputes.filter(
            Q(zd_ticket_id__icontains=search_query) |
            Q(buyer_email__icontains=search_query) |
            Q(paypal_dispute_id__icontains=search_query)
        )

    # Pagination
    paginator = Paginator(disputes, 20)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # Get status choices for filter dropdown
    status_choices = Dispute.STATUS_CHOICES

    # Count disputes by status for quick stats
    status_counts = Dispute.objects.values('status').annotate(
        count=Count('id')
    )
    status_count_map = {item['status']: item['count'] for item in status_counts}

    # Build list of (label, count) for template rendering
    status_summary = [
        {'label': label, 'count': status_count_map.get(code, 0)}
        for code, label in status_choices
    ]

    # Get Zendesk subdomain for ticket links
    try:
        from apps.config.models import SystemSettings
        zd_subdomain = SystemSettings.get_instance().zd_subdomain
    except Exception:
        zd_subdomain = ''

    context = {
        'page_obj': page_obj,
        'disputes': page_obj,
        'status_filter': status_filter,
        'search_query': search_query,
        'status_choices': status_choices,
        'status_summary': status_summary,
        'zd_subdomain': zd_subdomain,
    }

    return render(request, 'manager/disputes.html', context)


@manager_required
def dispute_detail(request, dispute_id):
    """
    Display full dispute details with screenshots, documents, and activity log.

    GET /manager/disputes/<id>/

    Template: manager/dispute_detail.html
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)

    # Get related data
    screenshots = DisputeScreenshot.objects.filter(dispute=dispute).order_by('-captured_at')
    documents = DisputeDocument.objects.filter(dispute=dispute).order_by('-created_at')
    activity_log = DisputeActivityLog.objects.filter(dispute=dispute).order_by('-performed_at')[:50]

    # Get claim evidence if claim exists
    claim_evidence = []
    if dispute.claim:
        claim_evidence = dispute.claim.evidence.all()[:10]

    context = {
        'dispute': dispute,
        'screenshots': screenshots,
        'documents': documents,
        'activity_log': activity_log,
        'claim_evidence': claim_evidence,
    }

    return render(request, 'manager/dispute_detail.html', context)


@manager_required
@require_POST
def dispute_generate_documents(request, dispute_id):
    """
    Generate dispute documents (response letter and evidence report).

    POST /manager/disputes/<id>/generate-documents/

    Triggers document generation service for both document types.
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)

    # Validate dispute has Zendesk ticket
    if not dispute.zd_ticket_id:
        messages.error(request, f"Cannot generate documents: Dispute #{dispute_id} has no Zendesk ticket linked.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    try:
        # Generate response letter
        response_letter = generate_response_letter(dispute_id)
        if response_letter:
            messages.success(request, f"Response letter generated successfully (Document #{response_letter.id})")
        else:
            messages.warning(request, "Failed to generate response letter")

        # Generate evidence report
        evidence_report = generate_evidence_report(dispute_id)
        if evidence_report:
            messages.success(request, f"Evidence report generated successfully (Document #{evidence_report.id})")
        else:
            messages.warning(request, "Failed to generate evidence report")

    except Exception as e:
        logger.error(f"Error generating documents for Dispute #{dispute_id}: {e}")
        messages.error(request, f"Error generating documents: {str(e)}")

    return redirect('disputes:dispute_detail', dispute_id=dispute_id)


@manager_required
def dispute_edit_document(request, document_id):
    """
    Edit document content inline with HTML editor.

    GET /manager/documents/<id>/edit/ - Display editor
    POST /manager/documents/<id>/edit/ - Save changes

    Template: manager/dispute_edit_document.html
    """
    document = get_object_or_404(DisputeDocument, pk=document_id)

    if request.method == 'POST':
        content_html = request.POST.get('content_html', '')
        version_increment = request.POST.get('version_increment', 'on')

        # Update document content
        document.content_html = content_html

        # Increment version if requested
        if version_increment:
            document.version += 1

        document.save()

        # Log the activity
        DisputeActivityLog.objects.create(
            dispute=document.dispute,
            action='NOTE_ADDED',
            details=f"Document #{document.id} edited. New version: {document.version}",
        )

        messages.success(request, f"Document #{document_id} updated successfully (v{document.version})")
        return redirect('disputes:dispute_detail', dispute_id=document.dispute_id)

    context = {
        'document': document,
        'dispute': document.dispute,
        'safe_preview': sanitize_document_html(document.content_html),
    }

    return render(request, 'manager/dispute_edit_document.html', context)


@manager_required
@require_POST
def dispute_accept_document(request, document_id):
    """
    Accept a document (mark as ready for submission).

    POST /manager/documents/<id>/accept/

    Changes document status to ACCEPTED and records acceptance timestamp/user.
    """
    document = get_object_or_404(DisputeDocument, pk=document_id)

    # Update document status
    document.status = 'ACCEPTED'
    document.accepted_at = None  # Will be set on save
    document.accepted_by = request.user
    document.save()

    # Log the activity
    DisputeActivityLog.objects.create(
        dispute=document.dispute,
        action='DOCUMENT_ACCEPTED',
        details=f"Document #{document.id} ({document.get_doc_type_display()}) accepted by {request.user.username}",
    )

    messages.success(request, f"Document #{document_id} accepted and ready for submission.")
    return redirect('disputes:dispute_detail', dispute_id=document.dispute_id)


@manager_required
@require_POST
def dispute_delete_document(request, document_id):
    """
    Delete a document.

    POST /manager/documents/<id>/delete/

    Removes the document record and associated file.
    """
    document = get_object_or_404(DisputeDocument, pk=document_id)
    dispute_id = document.dispute_id

    # Log before deletion
    DisputeActivityLog.objects.create(
        dispute=document.dispute,
        action='NOTE_ADDED',
        details=f"Document #{document.id} ({document.get_doc_type_display()}) deleted by {request.user.username}",
    )

    # Delete the document (file will be deleted by Django's file storage)
    document.delete()

    messages.success(request, f"Document #{document_id} deleted successfully.")
    return redirect('disputes:dispute_detail', dispute_id=dispute_id)


@manager_required
@require_POST
def dispute_set_category(request, dispute_id):
    """Set the dispute's category (PayPal reason) — the human's pick that
    drives which evidence report is used.

    POST /manager/disputes/<id>/set-category/  {category}
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)
    category = request.POST.get('category', '').strip()
    if category not in dict(Dispute.REASON_CHOICES):
        messages.error(request, "Unknown dispute category.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)
    dispute.dispute_reason = category
    dispute.save(update_fields=['dispute_reason', 'updated_at'])
    DisputeActivityLog.objects.create(
        dispute=dispute, action='STATUS_CHANGED', performed_by=request.user,
        details=f"Category set to {dispute.get_dispute_reason_display()}.")
    messages.success(request, f"Category set to {dispute.get_dispute_reason_display()}.")
    return redirect('disputes:dispute_detail', dispute_id=dispute_id)


@manager_required
@require_POST
def dispute_send_evidence(request, dispute_id):
    """
    Send evidence to PayPal for a dispute.

    POST /manager/disputes/<id>/send-evidence/

    Collects all ACCEPTED documents and submits them via PayPal API.
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)

    # Stage gate: PayPal rejects evidence at the INQUIRY stage (message-only)
    # and once the case is resolved. Refuse before calling PayPal.
    if not dispute.can_submit_evidence:
        messages.error(
            request,
            "Evidence can't be submitted yet: PayPal only accepts it once the "
            "dispute reaches the chargeback stage (it's currently "
            f"'{dispute.dispute_life_cycle_stage or 'unknown'}'), and not after "
            "the case is resolved.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    # Get all accepted documents
    accepted_documents = DisputeDocument.objects.filter(
        dispute=dispute,
        status='ACCEPTED'
    )

    if not accepted_documents.exists():
        messages.error(request, "No accepted documents to submit. Please generate and accept documents first.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    # Build response text from document content
    response_texts = []
    for doc in accepted_documents:
        if doc.content_html:
            response_texts.append(f"=== {doc.get_doc_type_display()} (v{doc.version}) ===\n{doc.content_html}")

    response_text = "\n\n".join(response_texts) if response_texts else "Evidence submitted for dispute resolution."

    try:
        # Call PayPal API to provide evidence
        success = provide_evidence(
            dispute_id=dispute.paypal_dispute_id,
            documents=list(accepted_documents),
            response_text=response_text,
        )

        if success:
            messages.success(request, f"Evidence successfully submitted to PayPal for Dispute #{dispute_id}.")
        else:
            messages.error(request, "Failed to submit evidence to PayPal. Check logs for details.")

    except Exception as e:
        logger.error(f"Error sending evidence for Dispute #{dispute_id}: {e}")
        messages.error(request, f"Error submitting evidence: {str(e)}")

    return redirect('disputes:dispute_detail', dispute_id=dispute_id)


@manager_required
@require_POST
def dispute_accept_claim(request, dispute_id):
    """
    Accept a dispute claim (issue refund) via PayPal API.

    POST /manager/disputes/<id>/accept-claim/

    Accepts the dispute and issues a refund to the buyer.
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)

    # State guard: don't accept an already-resolved/accepted dispute (would be
    # a no-op at best, a confusing double-action at worst).
    if dispute.status in ('RESOLVED_WON', 'RESOLVED_LOST', 'ACCEPTED'):
        messages.error(
            request,
            f"This dispute is already {dispute.get_status_display()} — nothing to accept.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    # Get optional note from POST data
    note = request.POST.get('note', '')

    try:
        # Call PayPal API to accept claim
        success = accept_claim(
            dispute_id=dispute.paypal_dispute_id,
            note=note,
        )

        if success:
            messages.success(request, f"Claim accepted for Dispute #{dispute_id}. Refund issued to buyer.")
        else:
            messages.error(request, "Failed to accept claim via PayPal API. Check logs for details.")

    except Exception as e:
        logger.error(f"Error accepting claim for Dispute #{dispute_id}: {e}")
        messages.error(request, f"Error accepting claim: {str(e)}")

    return redirect('disputes:dispute_detail', dispute_id=dispute_id)


@manager_required
@require_POST
def dispute_capture_screenshots(request, dispute_id):
    """
    Trigger screenshot capture for a dispute.

    POST /manager/disputes/<id>/capture-screenshots/

    Calls the screenshot service to capture Zendesk ticket screenshots.
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)

    # Validate dispute has Zendesk ticket
    if not dispute.zd_ticket_id:
        messages.error(request, f"Cannot capture screenshots: Dispute #{dispute_id} has no Zendesk ticket linked.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    try:
        # Call screenshot service
        success, message = capture_screenshots_manual(dispute_id)

        if success:
            messages.success(request, f"Screenshot captured: {message}")
        else:
            messages.error(request, f"Screenshot capture failed: {message}")

    except Exception as e:
        logger.error(f"Error capturing screenshots for Dispute #{dispute_id}: {e}")
        messages.error(request, f"Error capturing screenshots: {str(e)}")

    return redirect('disputes:dispute_detail', dispute_id=dispute_id)
