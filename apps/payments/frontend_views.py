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

from decimal import Decimal, InvalidOperation
from django.db import IntegrityError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.users.decorators import manager_required
from apps.claims.models import Claim
from apps.payments.models import Dispute, DisputeDocument, DisputeScreenshot, DisputeActivityLog
from apps.payments.document_service import generate_response_letter, generate_evidence_report
from apps.payments.paypal_disputes_service import provide_evidence, accept_claim

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


def strip_active_html(html: str) -> str:
    """Remove only executable content (<script> blocks and on*= handlers) while
    PRESERVING layout (tables, images, inline styles). Used when re-rendering an
    edited EVIDENCE_REPORT to PDF — the strict allowlist sanitizer would destroy
    the report's tables/images/styles. Manager-only edit → PDF, so this is enough."""
    import re
    html = re.sub(r'<script[^>]*>.*?</script>', '', html or '', flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'\son\w+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', '', html, flags=re.IGNORECASE)
    return html


def _parse_due(raw: str):
    """Parse a date or datetime string from the form into an aware datetime."""
    raw = (raw or '').strip()
    if not raw:
        return None
    dt = parse_datetime(raw)
    if dt is None:
        d = parse_date(raw)
        if d:
            from datetime import datetime, time
            dt = datetime.combine(d, time(23, 59))
    if dt is not None and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


@manager_required
def dispute_create(request):
    """Manually create a dispute from a claim — the fallback for when PayPal's
    webhook never delivered the dispute. Prefills from the claim; the manager
    picks the category and (optionally) the real PayPal IDs / response deadline.
    Then the normal flow (Generate Documents → edit → download/send) takes over.

    GET  /manager/disputes/create/?claim=<id>  - prefilled form
    POST /manager/disputes/create/             - create and redirect to detail
    """
    claim_id = request.GET.get('claim') or request.POST.get('claim_id')
    claim = Claim.objects.filter(pk=claim_id).first() if claim_id else None
    if not claim:
        messages.error(request, "Pick a claim to create a dispute from.")
        return redirect('disputes:dispute_list')

    if request.method == 'POST':
        reason = request.POST.get('dispute_reason', '')
        reason = reason if reason in Dispute.VALID_REASONS else ''

        buyer_email = (request.POST.get('buyer_email', '') or claim.client_email or '').strip()
        if not buyer_email:
            messages.error(request, "This claim has no client email; enter a buyer email to create the dispute.")
            return redirect(f"{request.path}?claim={claim.id}")

        ppid = (request.POST.get('paypal_dispute_id', '') or '').strip()
        if not ppid:
            ppid = f"MANUAL-{claim.zd_ticket_id or claim.id}-{int(timezone.now().timestamp())}"

        amount = claim.price_paid
        raw_amount = (request.POST.get('dispute_amount', '') or '').strip()
        if raw_amount:
            try:
                amount = Decimal(raw_amount)
            except (InvalidOperation, ValueError):
                amount = claim.price_paid

        currency = ((request.POST.get('dispute_currency', '') or 'USD').strip()[:3] or 'USD').upper()

        try:
            dispute = Dispute.objects.create(
                paypal_dispute_id=ppid,
                paypal_case_id=(request.POST.get('paypal_case_id', '') or '').strip(),
                claim=claim,
                zd_ticket_id=claim.zd_ticket_id or '',
                status='MATCHED',
                dispute_reason=reason,
                dispute_amount=amount,
                dispute_currency=currency,
                buyer_email=buyer_email,
                buyer_name=claim.client_name or '',
                transaction_id=claim.woocommerce_id or 'MANUAL',
                transaction_date=claim.created_at or timezone.now(),
                seller_response_due=_parse_due(request.POST.get('seller_response_due', '')),
                notes='Manually created in LORA (PayPal dispute did not arrive via webhook).',
            )
        except IntegrityError:
            messages.error(request, f"A dispute with PayPal ID '{ppid}' already exists.")
            return redirect(f"{request.path}?claim={claim.id}")

        DisputeActivityLog.objects.create(
            dispute=dispute, action='STATUS_CHANGED',
            details=f"Dispute manually created from claim #{claim.id} "
                    f"({claim.alf_claim_id or 'no ALF id'}).")
        messages.success(
            request,
            f"Dispute #{dispute.id} created. Generate the evidence report below, then download or send it.")
        return redirect('disputes:dispute_detail', dispute_id=dispute.id)

    context = {'claim': claim, 'reason_choices': Dispute.REASON_CHOICES}
    return render(request, 'manager/dispute_create.html', context)


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

        # Don't wipe the document if the editor posted empty content (e.g. the
        # in-place editor's JS failed to serialise the iframe).
        if content_html.strip():
            document.content_html = content_html

        # Increment version if requested
        if version_increment:
            document.version += 1

        document.save()

        # Re-render the PDF from the edited HTML so the file we submit to PayPal
        # reflects the manager's edits. Evidence reports are full HTML we can
        # render directly (strip only scripts/handlers, preserve layout).
        regenerated = False
        if document.doc_type == 'EVIDENCE_REPORT' and content_html.strip():
            try:
                from apps.payments.document_service import _render_to_pdf
                from django.core.files.base import ContentFile
                pdf_bytes = _render_to_pdf(
                    strip_active_html(content_html),
                    f"Dispute #{document.dispute_id} Evidence Report (edited)")
                if pdf_bytes:
                    document.file_path.save(
                        f"evidence_report_dispute_{document.dispute_id}_v{document.version}_edited.pdf",
                        ContentFile(pdf_bytes), save=True)
                    regenerated = True
            except Exception as e:
                logger.error(f"Failed to re-render edited PDF for document #{document.id}: {e}")

        # Log the activity
        DisputeActivityLog.objects.create(
            dispute=document.dispute,
            action='NOTE_ADDED',
            details=f"Document #{document.id} edited (v{document.version}); "
                    f"PDF {'regenerated' if regenerated else 'not regenerated'}.",
        )

        if regenerated:
            messages.success(request, f"Document #{document_id} updated and PDF regenerated (v{document.version}).")
        else:
            messages.success(request, f"Document #{document_id} updated successfully (v{document.version}).")
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

    # Update document status. (accepted_at is a plain DateTimeField with no
    # auto-set — it must be stamped explicitly; it was being left None forever.)
    document.status = 'ACCEPTED'
    document.accepted_at = timezone.now()
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
