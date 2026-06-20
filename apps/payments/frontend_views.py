"""
Frontend views for Dispute Management (authenticated staff only).

(@manager_required is now just a login gate — the manager/agent role split was
removed in favour of a single trusted-staff user type.)

Provides UI views for managing PayPal disputes:
- List disputes with filters
- View dispute details
- Generate/edit/accept/delete documents
- Send evidence to PayPal
- Accept claims
- Capture screenshots
"""

import json
import logging
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.db.models import Q, Count
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST

from decimal import Decimal, InvalidOperation
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from django.core.exceptions import ValidationError

from apps.users.decorators import manager_required
from apps.claims.models import Claim
from apps.claims.services import validate_evidence_attachment
from apps.payments.models import (Dispute, DisputeDocument, DisputeActivityLog,
                                  DisputeSubmission, DisputeSubmissionImage)
from apps.payments.document_service import (generate_evidence_report,
                                            build_dispute_narrative_notes, build_dispute_reply_timeline,
                                            PAYPAL_NOTES_MAX_CHARS)
from apps.payments.paypal_disputes_service import (accept_claim,
                                                   submit_dispute_response, evidence_type_for_reason)

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


# --- PayPal state normalization (single source of truth for the queues) -------
# PayPal exposes its state in TWO payload keys — `status`
# (WAITING_FOR_SELLER_RESPONSE / UNDER_REVIEW / RESOLVED) and `dispute_state`
# (REQUIRED_ACTION / UNDER_PAYPAL_REVIEW / RESOLVED). Treat BOTH as authoritative
# so a payload carrying only one still lands in the right queue (matches
# Dispute.submit_endpoint, which already reads both).

def _pp_under_review_q():
    """Dispute is under PayPal review — by EITHER payload key."""
    return (Q(raw_webhook_payload__status='UNDER_REVIEW') |
            Q(raw_webhook_payload__dispute_state='UNDER_PAYPAL_REVIEW'))


def _pp_resolved_q():
    """Dispute is resolved/closed at PayPal — by EITHER payload key."""
    return (Q(raw_webhook_payload__status='RESOLVED') |
            Q(raw_webhook_payload__dispute_state='RESOLVED'))


def _needs_action_qs(qs):
    """Disputes still needing a reply: not dormant at PayPal (under review OR
    resolved, by either key) and not in a LORA terminal state. has_key-safe so
    payload-less (manually created) disputes aren't dropped by the
    SQL-NULL-in-exclude trap."""
    status_ok = (~Q(raw_webhook_payload__has_key='status') |
                 ~Q(raw_webhook_payload__status__in=['UNDER_REVIEW', 'RESOLVED']))
    state_ok = (~Q(raw_webhook_payload__has_key='dispute_state') |
                ~Q(raw_webhook_payload__dispute_state__in=['UNDER_PAYPAL_REVIEW', 'RESOLVED']))
    return qs.filter(status_ok & state_ok).exclude(status__in=Dispute.TERMINAL_STATUSES)


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

        # Create the dispute and its creation-log entry as one unit — a crash
        # between them would leave a dispute with no audit trail.
        try:
            with transaction.atomic():
                dispute = Dispute.objects.create(
                    paypal_dispute_id=ppid,
                    paypal_case_id=(request.POST.get('paypal_case_id', '') or '').strip(),
                    claim=claim,
                    zd_ticket_id=claim.zd_ticket_id or '',
                    status=Dispute.STATUS_MATCHED,
                    dispute_reason=reason,
                    dispute_amount=amount,
                    dispute_currency=currency,
                    buyer_email=buyer_email,
                    buyer_name=claim.client_name or '',
                    # The PayPal transaction id (used for matching + cross-checks),
                    # NOT the WooCommerce order id. Prefer the real PayPal txn id;
                    # fall back to the order id, then a marker.
                    transaction_id=(claim.paypal_transaction_id or claim.woocommerce_id or 'MANUAL'),
                    transaction_date=claim.created_at or timezone.now(),
                    seller_response_due=_parse_due(request.POST.get('seller_response_due', '')),
                    notes='Manually created in LORA (PayPal dispute did not arrive via webhook).',
                )
                DisputeActivityLog.objects.create(
                    dispute=dispute, action=DisputeActivityLog.ACTION_STATUS_CHANGED,
                    details=f"Dispute manually created from claim #{claim.id} "
                            f"({claim.alf_claim_id or 'no ALF id'}).")
        except IntegrityError:
            messages.error(request, f"A dispute with PayPal ID '{ppid}' already exists.")
            return redirect(f"{request.path}?claim={claim.id}")

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
    view = (request.GET.get('view') or 'action').lower()

    disputes = Dispute.objects.all()

    # Apply search filter (ticket ID or email)
    if search_query:
        disputes = disputes.filter(
            Q(zd_ticket_id__icontains=search_query) |
            Q(buyer_email__icontains=search_query) |
            Q(paypal_dispute_id__icontains=search_query)
        )

    # View / status filter. An explicit LORA-status drilldown wins; otherwise the
    # `view` decides — default 'action' shows ONLY disputes that still need a reply.
    if status_filter:
        disputes = disputes.filter(status=status_filter)
    elif view == 'review':
        disputes = disputes.filter(_pp_under_review_q())
    elif view == 'resolved':
        disputes = disputes.filter(_pp_resolved_q() | Q(status__in=Dispute.TERMINAL_STATUSES))
    elif view == 'all':
        pass
    else:
        view = 'action'
        disputes = _needs_action_qs(disputes)

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

    # Counts for the view tabs (independent of the search/status drilldown).
    _all = Dispute.objects.all()
    view_counts = {
        'action': _needs_action_qs(_all).count(),
        'review': _all.filter(_pp_under_review_q()).count(),
        'resolved': _all.filter(_pp_resolved_q() | Q(status__in=Dispute.TERMINAL_STATUSES)).count(),
        'all': _all.count(),
    }

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
        'view': view,
        'view_counts': view_counts,
        'status_choices': status_choices,
        'status_summary': status_summary,
        'zd_subdomain': zd_subdomain,
        # Disputes with no claim attached — surfaced as a "needs linking" banner.
        'unmatched_count': Dispute.objects.filter(claim__isnull=True).count(),
        # Disputes PayPal has already closed — offered for one-click cleanup.
        'resolved_count': Dispute.objects.filter(_pp_resolved_q()).count(),
    }

    return render(request, 'manager/disputes.html', context)


@manager_required
@require_POST
def dispute_pull_from_paypal(request):
    """Backfill disputes that predate the webhook: list them from PayPal and
    ingest each (best-effort match to a claim by buyer email). Disputes with no
    matching claim land as RECEIVED for manual linking. Manager-triggered button
    on the dispute list."""
    from apps.payments.paypal_disputes_service import list_paypal_disputes, ingest_dispute

    dispute_ids = list_paypal_disputes()
    if not dispute_ids:
        messages.warning(
            request,
            "No disputes returned from PayPal. Check that the PayPal credentials have "
            "Disputes-API permission and that you're in the right mode (live vs sandbox).")
        return redirect('disputes:dispute_list')

    created, existing, unmatched, failed = 0, 0, 0, 0
    for did in dispute_ids:
        try:
            dispute, was_created = ingest_dispute(did)
        except Exception as e:  # one bad dispute must not abort the batch
            logger.error(f"Pull-from-PayPal: ingest failed for {did}: {e}", exc_info=True)
            failed += 1
            continue
        if dispute is None:
            failed += 1
            continue
        created += 1 if was_created else 0
        existing += 0 if was_created else 1
        if dispute.claim_id is None:
            unmatched += 1

    messages.success(
        request,
        f"Pulled {len(dispute_ids)} dispute(s) from PayPal: {created} new, {existing} already known.")
    if unmatched:
        messages.warning(
            request,
            f"{unmatched} dispute(s) have no matching claim yet — open each and use "
            "“Link to claim” (filter by status “Received”).")
    if failed:
        messages.warning(request, f"{failed} dispute(s) could not be read from PayPal.")
    return redirect('disputes:dispute_list')


@manager_required
@require_POST
def dispute_prune_resolved(request):
    """Delete disputes that PayPal has already RESOLVED/closed — pulled in for
    completeness but needing no action. Identified by the PayPal status stored in
    raw_webhook_payload, so ONLY PayPal-pulled disputes are touched (manually
    created ones have an empty payload and are never matched). Cascades remove
    their documents/activity log."""
    resolved = Dispute.objects.filter(_pp_resolved_q())
    count = resolved.count()
    if not count:
        messages.info(request, "No resolved disputes to remove.")
        return redirect('disputes:dispute_list')
    resolved.delete()
    messages.success(
        request, f"Removed {count} dispute(s) already resolved/closed at PayPal.")
    return redirect('disputes:dispute_list')


@manager_required
@require_POST
def dispute_link_claim(request, dispute_id):
    """Attach an unmatched dispute to an existing claim by reference (Zendesk
    ticket id, ALF claim id, client email, or LORA claim id). Flips RECEIVED→
    MATCHED and logs it. The fallback for disputes whose PayPal buyer email
    didn't auto-match a claim."""
    dispute = get_object_or_404(Dispute, pk=dispute_id)
    if dispute.claim_id:
        messages.info(request, "This dispute is already linked to a claim.")
        return redirect('disputes:dispute_detail', dispute_id=dispute.id)

    ref = (request.POST.get('claim_ref') or '').strip()
    if not ref:
        messages.warning(request, "Enter a claim reference (Zendesk ticket ID, ALF claim ID, or email).")
        return redirect('disputes:dispute_detail', dispute_id=dispute.id)

    lookup = Q(zd_ticket_id=ref) | Q(alf_claim_id__iexact=ref) | Q(client_email__iexact=ref)
    if ref.isdigit():
        lookup |= Q(pk=int(ref))
    matches = list(Claim.objects.filter(lookup)[:6])
    if not matches:
        messages.warning(request, f"No claim found matching '{ref}'.")
        return redirect('disputes:dispute_detail', dispute_id=dispute.id)
    if len(matches) > 1:
        messages.warning(
            request,
            f"'{ref}' matches {len(matches)} claims — use a more specific reference "
            "(Zendesk ticket ID or ALF claim ID).")
        return redirect('disputes:dispute_detail', dispute_id=dispute.id)

    claim = matches[0]

    # Transaction-id cross-check (same key auto-matching uses): if BOTH sides
    # carry a PayPal transaction id and they DISAGREE, refuse the link unless the
    # manager explicitly confirms — linking the wrong claim mis-attributes a
    # dispute to another customer's Zendesk case. Manual linking is the intended
    # override, but a mismatch must be a deliberate, ticked choice.
    dispute_txn = (dispute.transaction_id or '').strip()
    claim_txn = (claim.paypal_transaction_id or '').strip()
    txn_mismatch = bool(dispute_txn and claim_txn and dispute_txn != claim_txn)
    if txn_mismatch and not request.POST.get('override'):
        messages.error(
            request,
            f"Not linked — the PayPal transaction IDs differ (dispute {dispute_txn} vs "
            f"claim {claim_txn}). If this really is the right claim, tick "
            "“Link even if transaction IDs differ” and try again.")
        return redirect('disputes:dispute_detail', dispute_id=dispute.id)

    dispute.claim = claim
    update_fields = ['claim', 'updated_at']
    if not dispute.zd_ticket_id and claim.zd_ticket_id:
        dispute.zd_ticket_id = claim.zd_ticket_id
        update_fields.append('zd_ticket_id')
    if dispute.status == Dispute.STATUS_RECEIVED:
        dispute.status = Dispute.STATUS_MATCHED
        update_fields.append('status')
    # Link + audit-log as one unit (no linked dispute without its log entry).
    with transaction.atomic():
        dispute.save(update_fields=update_fields)
        DisputeActivityLog.objects.create(
            dispute=dispute, action=DisputeActivityLog.ACTION_DISPUTE_MATCHED,
            details=(f"Manually linked to claim #{claim.id} ({claim.alf_claim_id}) by {request.user}"
                     + (" — OVERRIDE: transaction ids differ." if txn_mismatch else ".")))
    messages.success(request, f"Linked dispute to claim #{claim.id} ({claim.alf_claim_id}).")
    if txn_mismatch:
        messages.warning(request, "Linked despite differing PayPal transaction IDs (manager override).")
    return redirect('disputes:dispute_detail', dispute_id=dispute.id)


@manager_required
def dispute_detail(request, dispute_id):
    """
    Display full dispute details with documents and activity log.

    GET /manager/disputes/<id>/

    Template: manager/dispute_detail.html
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)

    # Get related data
    documents = DisputeDocument.objects.filter(dispute=dispute).order_by('-created_at')
    activity_log = DisputeActivityLog.objects.filter(dispute=dispute).order_by('-performed_at')[:50]

    # Get claim evidence if claim exists
    claim_evidence = []
    if dispute.claim:
        claim_evidence = dispute.claim.evidence.all()[:10]

    # Pretty-printed raw PayPal response (for the manager-only debug viewer —
    # lets us verify field mapping against what PayPal actually returns).
    raw_payload_json = json.dumps(
        dispute.raw_webhook_payload or {}, indent=2, sort_keys=True, default=str)

    # Zendesk subdomain for the ticket link (was missing — links rendered as
    # https://.zendesk.com/...). Mirrors dispute_list.
    try:
        from apps.config.models import SystemSettings
        zd_subdomain = SystemSettings.get_instance().zd_subdomain
    except Exception:
        zd_subdomain = ''

    # Back-and-forth: the working draft submission being prepared, the merged
    # reply timeline, and whether a generated evidence PDF exists to attach.
    draft_submission = _working_draft(dispute)
    has_evidence_pdf = (DisputeDocument.objects
                        .filter(dispute=dispute, doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT)
                        .exclude(file_path='').exists())

    # When PayPal isn't accepting a reply (submit_endpoint == ''), explain WHY in
    # the manager's terms — a bare "No reply window open" reads like a bug.
    submit_endpoint = dispute.submit_endpoint
    reply_window_reason = ''
    if not submit_endpoint:
        payload = dispute.raw_webhook_payload or {}
        pp_status = (payload.get('status') or '').upper()
        pp_state = (payload.get('dispute_state') or '').upper()
        stage = (dispute.dispute_life_cycle_stage or '').upper()
        if dispute.status in Dispute.TERMINAL_STATUSES or 'RESOLVED' in (pp_status, pp_state):
            reply_window_reason = "This dispute is resolved — there's nothing left to send."
        elif pp_status == 'WAITING_FOR_BUYER_RESPONSE' or pp_state == 'REQUIRED_OTHER_PARTY_ACTION':
            reply_window_reason = (
                "PayPal is waiting for the buyer to respond. Your evidence is already on "
                "file — there's nothing to send right now. You'll be able to reply again if "
                "PayPal escalates this to a claim or asks you for more.")
        elif stage == 'INQUIRY':
            reply_window_reason = (
                "This is still a PayPal inquiry (before chargeback). PayPal only accepts a "
                "formal evidence reply once it escalates to a claim — the reply box opens then. "
                "To message the buyer meanwhile, use PayPal's Resolution Center directly.")
        else:
            reply_window_reason = (
                "PayPal isn't accepting a reply right now (the case may be mid-review or "
                "already resolved). You can still draft ahead.")

    context = {
        'dispute': dispute,
        'documents': documents,
        'activity_log': activity_log,
        'claim_evidence': claim_evidence,
        'raw_payload_json': raw_payload_json,
        'zd_subdomain': zd_subdomain,
        'draft_submission': draft_submission,
        'reply_timeline': build_dispute_reply_timeline(dispute),
        'submit_endpoint': submit_endpoint,
        'reply_window_reason': reply_window_reason,
        'has_evidence_pdf': has_evidence_pdf,
        'evidence_type_default': evidence_type_for_reason(dispute.dispute_reason),
        # Soft cap surfaced in the composer's live counter (PayPal caps the notes
        # field near here; the service also warns past it).
        'paypal_notes_max': PAYPAL_NOTES_MAX_CHARS,
    }

    return render(request, 'manager/dispute_detail.html', context)


def _working_draft(dispute):
    """The dispute's current DRAFT submission being prepared (latest), or None."""
    return dispute.submissions.filter(
        status=DisputeSubmission.STATUS_DRAFT).order_by('-created_at').first()


@manager_required
@require_POST
def dispute_refresh_from_paypal(request, dispute_id):
    """Pull THIS dispute's latest state from PayPal on demand — refreshes the
    stage, deadline, status, AND the stored raw payload that the conversation
    thread reads the buyer/PayPal messages + evidences from. Without this the
    thread only updates after our own submit or an inbound webhook."""
    dispute = get_object_or_404(Dispute, pk=dispute_id)
    if (dispute.paypal_dispute_id or '').startswith('MANUAL-'):
        messages.info(request, "This dispute was created manually — there's nothing to refresh from PayPal.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)
    try:
        from apps.payments.paypal_disputes_service import sync_dispute_from_paypal
        sync_dispute_from_paypal(dispute.paypal_dispute_id)
    except Exception as e:
        logger.error(f"Refresh-from-PayPal failed for Dispute #{dispute_id}: {e}")
        messages.error(request, f"Couldn't refresh from PayPal: {e}")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)
    messages.success(request, "Refreshed from PayPal — the thread shows the latest messages and status.")
    return redirect('disputes:dispute_detail', dispute_id=dispute_id)


@manager_required
@require_POST
def dispute_generate_documents(request, dispute_id):
    """
    Generate the dispute EVIDENCE REPORT (the only generated document now — the
    written argument to PayPal is plain text on a submission, not a PDF letter).

    POST /manager/disputes/<id>/generate-documents/
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)

    # Validate dispute has Zendesk ticket
    if not dispute.zd_ticket_id:
        messages.error(request, f"Cannot generate the evidence report: Dispute #{dispute_id} has no Zendesk ticket linked.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    try:
        evidence_report = generate_evidence_report(dispute_id)
        if evidence_report:
            messages.success(request, f"Evidence report generated successfully (Document #{evidence_report.id})")
        else:
            messages.warning(request, "Failed to generate evidence report")
    except Exception as e:
        logger.error(f"Error generating evidence report for Dispute #{dispute_id}: {e}")
        messages.error(request, f"Error generating evidence report: {str(e)}")

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
        # in-place editor's JS failed to serialise the iframe). Store the
        # SANITIZED HTML (scripts/handlers stripped) — the edit view re-renders
        # content_html into a srcdoc iframe, so persisting raw editor output
        # would allow stored XSS in the manager's session.
        if content_html.strip():
            document.content_html = strip_active_html(content_html)

        # Increment version if requested
        if version_increment:
            document.version += 1

        document.save()

        # Re-render the PDF from the edits so the file we attach to a PayPal
        # submission reflects the manager's changes. Only the EVIDENCE REPORT is
        # PDF-backed now (the response letter was dropped — its argument is plain
        # text on a submission). A legacy RESPONSE_LETTER row just saves its
        # content_html above and is not re-rendered (its template is gone).
        regenerated = False
        if content_html.strip() and document.doc_type == DisputeDocument.DOC_TYPE_EVIDENCE_REPORT:
            try:
                from apps.payments.document_service import _render_to_pdf
                from django.core.files.base import ContentFile
                # Evidence reports are full HTML — render the edited body directly
                # (strip only scripts/handlers, preserve layout).
                pdf_bytes = _render_to_pdf(
                    strip_active_html(content_html),
                    f"Dispute #{document.dispute_id} Evidence Report (edited)")
                filename = (f"evidence_report_dispute_{document.dispute_id}"
                            f"_v{document.version}_edited.pdf")
                if pdf_bytes:
                    document.file_path.save(filename, ContentFile(pdf_bytes), save=True)
                    regenerated = True
            except Exception as e:
                logger.error(f"Failed to re-render edited PDF for document #{document.id}: {e}")

        # Log the activity
        DisputeActivityLog.objects.create(
            dispute=document.dispute,
            action=DisputeActivityLog.ACTION_NOTE_ADDED,
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
        # Sanitize at render time too (not just on save) so EXISTING rows saved
        # before the save-path fix can't execute a smuggled <script> in the
        # srcdoc editor. Layout-preserving strip (scripts/handlers only).
        'report_srcdoc': strip_active_html(document.content_html or ''),
    }

    return render(request, 'manager/dispute_edit_document.html', context)


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
        action=DisputeActivityLog.ACTION_NOTE_ADDED,
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
    # Use the cached VALID_REASONS dict (same validation as dispute_create) rather
    # than rebuilding dict(REASON_CHOICES) on every request.
    if category not in Dispute.VALID_REASONS:
        messages.error(request, "Unknown dispute category.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)
    dispute.dispute_reason = category
    # Category change + its audit-log entry as one unit.
    with transaction.atomic():
        dispute.save(update_fields=['dispute_reason', 'updated_at'])
        DisputeActivityLog.objects.create(
            dispute=dispute, action=DisputeActivityLog.ACTION_STATUS_CHANGED,
            performed_by=request.user,
            details=f"Category set to {dispute.get_dispute_reason_display()}.")
    messages.success(request, f"Category set to {dispute.get_dispute_reason_display()}.")
    return redirect('disputes:dispute_detail', dispute_id=dispute_id)


@manager_required
@require_POST
def dispute_prepare_submission(request, dispute_id):
    """Prepare (draft) the submission to PayPal — feature B.

    Two actions on one form:
    - action=generate: (re)write the AI evidence narrative into the working
      DRAFT submission, using the manager's emphasis note.
    - action=save: store the manager's edits (narrative text, emphasis note,
      evidence_type, attach-PDF tick) plus any uploaded images.

    POST /manager/disputes/<id>/prepare-submission/
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)
    action = request.POST.get('action', 'save')
    draft = _working_draft(dispute)
    manager_note = (request.POST.get('manager_note') or '').strip()

    if action == 'generate':
        try:
            result = build_dispute_narrative_notes(dispute, manager_note=manager_note)
        except Exception as e:
            logger.error(f"Narrative generation failed for Dispute #{dispute_id}: {e}")
            messages.error(request, f"Could not generate the narrative: {e}")
            return redirect('disputes:dispute_detail', dispute_id=dispute_id)
        if draft is None:
            draft = DisputeSubmission(dispute=dispute)
        draft.notes = result['notes']
        draft.manager_note = manager_note
        draft.source = DisputeSubmission.SOURCE_AI  # machine-drafted (template fallback is still AI-origin)
        draft.status = DisputeSubmission.STATUS_DRAFT
        if not draft.evidence_type:
            draft.evidence_type = evidence_type_for_reason(dispute.dispute_reason)
        draft.save()
        if result['source'] == 'FALLBACK':
            messages.warning(request, "AI was unavailable — generated a template draft. Review it before submitting.")
        else:
            messages.success(request, "Draft narrative generated. Review and edit before submitting.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    # action == 'save'
    if draft is None:
        draft = DisputeSubmission(dispute=dispute, source=DisputeSubmission.SOURCE_MANUAL)
    notes = request.POST.get('notes', '')
    if draft.source == DisputeSubmission.SOURCE_AI and notes.strip() != (draft.notes or '').strip():
        draft.source = DisputeSubmission.SOURCE_AI_EDITED   # AI draft the manager then edited
    draft.notes = notes
    draft.manager_note = manager_note
    draft.attach_evidence_pdf = request.POST.get('attach_evidence_pdf') == 'on'
    evidence_type = (request.POST.get('evidence_type') or '').strip()
    if evidence_type:
        draft.evidence_type = evidence_type
    draft.status = DisputeSubmission.STATUS_DRAFT
    draft.save()

    saved = 0
    for f in request.FILES.getlist('images'):
        if not f:
            continue
        # Validate server-side (size + type) — don't trust the client's
        # content_type. Dispute attachments allow images AND PDFs (claim
        # evidence stays image-only).
        try:
            validate_evidence_attachment(f)
        except ValidationError as e:
            messages.error(request, f"{f.name}: {'; '.join(e.messages)}")
            continue
        DisputeSubmissionImage.objects.create(submission=draft, file=f, uploaded_by=request.user)
        saved += 1
    msg = "Submission draft saved."
    if saved:
        msg += f" {saved} file{'s' if saved != 1 else ''} attached."
    messages.success(request, msg)
    return redirect('disputes:dispute_detail', dispute_id=dispute_id)


@manager_required
@require_POST
def dispute_submit_to_paypal(request, dispute_id):
    """Submit the prepared DRAFT to PayPal — feature C. The endpoint
    (provide-evidence vs provide-supporting-info) is auto-picked by state.

    POST /manager/disputes/<id>/submit-to-paypal/
    """
    dispute = get_object_or_404(Dispute, pk=dispute_id)
    draft = _working_draft(dispute)
    if draft is None or not (draft.notes or '').strip():
        messages.error(request, "Prepare a submission first — generate or write the narrative, then save.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    endpoint = dispute.submit_endpoint
    if not endpoint:
        messages.error(
            request,
            "PayPal isn't accepting a submission for this dispute right now — it may "
            "still be at the inquiry stage (message-only) or already resolved.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    # Atomically claim the draft (DRAFT -> SUBMITTING) so two concurrent clicks
    # can't both POST the same submission to PayPal. Only the request whose
    # conditional update touches the row proceeds to the (outside-txn) call.
    claimed = (DisputeSubmission.objects
               .filter(pk=draft.pk, status=DisputeSubmission.STATUS_DRAFT)
               .update(status=DisputeSubmission.STATUS_SUBMITTING))
    if not claimed:
        messages.error(request, "This submission is already being sent — refresh to see its status.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)
    draft.refresh_from_db()

    try:
        ok = submit_dispute_response(draft, performed_by=request.user)
    except Exception as e:
        logger.error(f"Submit-to-PayPal failed for Dispute #{dispute_id}: {e}")
        # Release the claim so the manager can retry.
        (DisputeSubmission.objects
         .filter(pk=draft.pk, status=DisputeSubmission.STATUS_SUBMITTING)
         .update(status=DisputeSubmission.STATUS_DRAFT))
        messages.error(request, f"Error submitting to PayPal: {e}")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    if ok:
        messages.success(request, f"Submitted to PayPal via {endpoint}.")
    else:
        messages.error(request, "PayPal rejected the submission — see the timeline for the reason. "
                                "You can edit the draft and try again.")
    return redirect('disputes:dispute_detail', dispute_id=dispute_id)


@manager_required
@require_POST
def dispute_delete_submission_image(request, image_id):
    """Remove an image from a draft submission (before it is sent).

    POST /manager/disputes/submission-images/<id>/delete/
    """
    image = get_object_or_404(DisputeSubmissionImage, pk=image_id)
    dispute_id = image.submission.dispute_id
    if image.submission.status != DisputeSubmission.STATUS_DRAFT:
        messages.error(request, "Can't change attachments on an already-submitted entry.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)
    image.delete()
    messages.success(request, "Image removed from the draft.")
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
    if dispute.status in Dispute.TERMINAL_STATUSES:
        messages.error(
            request,
            f"This dispute is already {dispute.get_status_display()} — nothing to accept.")
        return redirect('disputes:dispute_detail', dispute_id=dispute_id)

    # Atomically claim this money-moving action (compare-and-set) so two
    # concurrent clicks can't both call PayPal's accept-claim. Only the request
    # that flips the flag proceeds; the flag is released when the call returns.
    claimed = dispute.claim_outbound(exclude_terminal=True)
    if not claimed:
        messages.error(request, "This dispute is already being processed — refresh to see its status.")
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
            # The call didn't confirm success. It may be a definite rejection OR
            # an indeterminate network failure where PayPal actually accepted and
            # refunded. Reconcile against PayPal (the source of truth) so the
            # local status + terminal-guard reflect reality before any retry —
            # if PayPal did accept, the dispute is now terminal and can't be
            # re-accepted.
            try:
                from apps.payments.paypal_disputes_service import sync_dispute_from_paypal
                sync_dispute_from_paypal(dispute.paypal_dispute_id)
            except Exception as sync_err:
                logger.warning(f"Post-accept reconcile sync failed for Dispute #{dispute_id}: {sync_err}")
            dispute.refresh_from_db()
            if dispute.status in Dispute.TERMINAL_STATUSES:
                messages.warning(
                    request,
                    "PayPal now reports this dispute as resolved — the acceptance went through "
                    "after all. The status has been updated; no need to retry.")
            else:
                messages.error(
                    request,
                    "Couldn't confirm the claim was accepted at PayPal. The status has been "
                    "re-synced — verify in PayPal before retrying to avoid a double refund.")

    except Exception as e:
        logger.error(f"Error accepting claim for Dispute #{dispute_id}: {e}")
        messages.error(request, f"Error accepting claim: {str(e)}")
    finally:
        # Release the in-flight claim (accept_claim sets ACCEPTED on success).
        dispute.release_outbound()

    return redirect('disputes:dispute_detail', dispute_id=dispute_id)
