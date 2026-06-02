"""
Document Generation Service for LORA Dispute Management.

Generates professional dispute response letters and evidence reports as PDF documents.
Uses Qwen AI for response letter generation and template-based rendering for evidence reports.
"""

import base64
import logging
import os
from datetime import datetime
from typing import Optional, Tuple

import bleach
from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from apps.payments.models import Dispute, DisputeDocument, DisputeScreenshot, DisputeActivityLog
from apps.config.models import SystemSettings
from apps.communications.models import EmailLog
from apps.claims.models import ClaimEvidence
from apps.integrations.services import fetch_zendesk_ticket_full, fetch_zendesk_comments

logger = logging.getLogger(__name__)

# Allowed HTML tags and attributes for sanitizing AI-generated content
ALLOWED_HTML_TAGS = ['p', 'br', 'strong', 'em', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'div', 'span', 'blockquote']
ALLOWED_HTML_ATTRIBUTES = {}


def _get_weasyprint():
    """
    Lazily import WeasyPrint to avoid import errors when library is not installed.
    
    Returns:
        Tuple of (HTML, CSS) classes, or (None, None) if not available.
    """
    try:
        from weasyprint import HTML, CSS
        return HTML, CSS
    except Exception as e:
        logger.error(f"WeasyPrint not available: {e}")
        logger.info("Install GTK+ and WeasyPrint: https://doc.courtbouillon.org/weasyprint/stable/first_steps.html")
        return None, None


def _call_qwen_ai(*, system_prompt: str, trusted: dict, untrusted: dict,
                  known_aliases: list[str]):
    """Generate a dispute response letter via the LLM. Returns (subject, body)."""
    from apps.ai.client import AIClient
    from apps.ai.schemas import DisputeLetter

    result = AIClient.complete(
        system_prompt=system_prompt,
        trusted=trusted,
        untrusted=untrusted,
        known_pii={"aliases": known_aliases},
        response_schema=DisputeLetter,
        call_site="dispute_letter",
        temperature=0.5,
        max_tokens=1500,
    )
    return result.subject, result.body


def _fetch_zendesk_ticket_full(zd_ticket_id: str) -> dict:
    """
    Fetch complete Zendesk ticket data including custom fields and comments.
    
    Args:
        zd_ticket_id: The Zendesk ticket ID
        
    Returns:
        Dictionary with ticket data and comments, or empty dict on failure
    """
    from apps.integrations.services import fetch_zendesk_ticket_full, fetch_zendesk_comments
    
    ticket_data = {}
    comments = []
    
    if zd_ticket_id:
        try:
            ticket_data = fetch_zendesk_ticket_full(zd_ticket_id) or {}
            comments = fetch_zendesk_comments(zd_ticket_id)
            logger.info(f"Fetched Zendesk ticket {zd_ticket_id} with {len(comments)} comments")
        except Exception as e:
            logger.error(f"Error fetching Zendesk ticket {zd_ticket_id}: {e}")
    
    return {
        'ticket': ticket_data,
        'comments': comments,
    }


def _encode_screenshot_to_base64(screenshot: DisputeScreenshot) -> Optional[str]:
    """
    Encode a screenshot image to base64 data URI for embedding in HTML/PDF.
    
    Args:
        screenshot: DisputeScreenshot instance
        
    Returns:
        Data URI string (data:image/...;base64,...) or None on failure
    """
    try:
        if not screenshot.image:
            return None
            
        # Open and read the image file
        screenshot.image.open('rb')
        image_data = screenshot.image.read()
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        
        # Determine MIME type from file extension
        file_ext = screenshot.image.name.split('.')[-1].lower()
        mime_type = {
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'png': 'image/png',
            'gif': 'image/gif',
            'webp': 'image/webp',
        }.get(file_ext, 'image/jpeg')
        
        data_uri = f"data:{mime_type};base64,{image_base64}"
        return data_uri
        
    except Exception as e:
        logger.warning(f"Error encoding screenshot {screenshot.id}: {e}")
        return None


def _fetch_claim_evidence_base64(claim) -> list:
    """
    Fetch all claim evidence images and encode as base64 data URIs.
    
    Args:
        claim: Claim instance
        
    Returns:
        List of dicts with description, uploaded_at, and data_uri
    """
    evidence_list = []
    
    for evidence in claim.evidence.all():
        try:
            if evidence.image:
                # Validate file path is within MEDIA_ROOT (prevent path traversal)
                abs_path = os.path.abspath(evidence.image.path)
                media_root = os.path.abspath(settings.MEDIA_ROOT)
                
                if not abs_path.startswith(media_root):
                    logger.warning(f"Evidence file path outside MEDIA_ROOT: {abs_path}")
                    continue
                
                # Open and encode image
                evidence.image.open('rb')
                image_data = evidence.image.read()
                image_base64 = base64.b64encode(image_data).decode('utf-8')
                
                # Determine MIME type
                file_ext = evidence.image.name.split('.')[-1].lower()
                mime_type = {
                    'jpg': 'image/jpeg',
                    'jpeg': 'image/jpeg',
                    'png': 'image/png',
                    'gif': 'image/gif',
                    'webp': 'image/webp',
                }.get(file_ext, 'image/jpeg')
                
                data_uri = f"data:{mime_type};base64,{image_base64}"
                
                evidence_list.append({
                    'description': evidence.description,
                    'uploaded_at': evidence.uploaded_at,
                    'data_uri': data_uri,
                })
        except Exception as e:
            logger.warning(f"Error processing evidence {evidence.id}: {e}")
            continue
    
    return evidence_list


def _fetch_communication_history(dispute: Dispute) -> list:
    """
    Fetch communication history (emails) related to the dispute's claim.
    
    Args:
        dispute: Dispute instance
        
    Returns:
        List of email log entries
    """
    emails = []
    
    if dispute.claim:
        try:
            email_logs = EmailLog.objects.filter(claim=dispute.claim).order_by('-received_at')[:50]
            for email_log in email_logs:
                emails.append({
                    'subject': email_log.subject,
                    'body': email_log.body,
                    'from_email': email_log.from_email,
                    'received_at': email_log.received_at,
                    'sentiment': email_log.sentiment,
                    'category': email_log.category,
                })
            logger.info(f"Fetched {len(emails)} emails for dispute {dispute.id}")
        except Exception as e:
            logger.error(f"Error fetching communication history for dispute {dispute.id}: {e}")
    
    return emails


def _render_to_pdf(html_string: str, filename_hint: str) -> Optional[bytes]:
    """
    Render HTML string to PDF bytes using WeasyPrint.
    
    Args:
        html_string: HTML content to render
        filename_hint: Hint for logging purposes
        
    Returns:
        PDF bytes or None on failure
    """
    HTML, CSS = _get_weasyprint()
    
    if not HTML:
        logger.error("WeasyPrint is not installed. Cannot generate PDF.")
        return None
    
    try:
        html = HTML(string=html_string)
        
        # CSS for professional document styling
        css = CSS(string='''
            @page {
                size: A4;
                margin: 2cm;
                @bottom-right {
                    content: "Page " counter(page) " of " counter(pages);
                    font-size: 10pt;
                    color: #666;
                }
                @bottom-left {
                    content: "LORA Dispute Document";
                    font-size: 9pt;
                    color: #888;
                }
            }
            
            body {
                font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
                line-height: 1.6;
                color: #333;
                font-size: 11pt;
            }
            
            /* Letter-specific styles */
            .letter-header {
                border-bottom: 2px solid #0070d2;
                padding-bottom: 15px;
                margin-bottom: 25px;
            }
            
            .letter-header h1 {
                color: #0070d2;
                margin: 0 0 10px 0;
                font-size: 18pt;
            }
            
            .letter-meta {
                font-size: 10pt;
                color: #666;
            }
            
            .customer-info, .transaction-info {
                background: #f8f9fa;
                padding: 15px;
                border-radius: 5px;
                margin-bottom: 20px;
                border-left: 4px solid #0070d2;
            }
            
            .response-body {
                margin: 25px 0;
                text-align: justify;
            }
            
            .response-body p {
                margin-bottom: 15px;
            }
            
            .letter-footer {
                margin-top: 40px;
                border-top: 1px solid #ddd;
                padding-top: 15px;
            }
            
            .signature {
                margin-top: 30px;
            }
            
            .signature-line {
                border-top: 1px solid #333;
                width: 250px;
                margin-top: 50px;
                padding-top: 5px;
            }
            
            /* Report-specific styles */
            .report-cover {
                text-align: center;
                padding: 60px 20px;
                page-break-after: always;
            }
            
            .report-cover h1 {
                color: #0070d2;
                font-size: 24pt;
                margin-bottom: 30px;
            }
            
            .report-cover .dispute-info {
                font-size: 12pt;
                color: #555;
                margin: 20px 0;
            }
            
            .section {
                margin-bottom: 30px;
                page-break-inside: avoid;
            }
            
            .section h2 {
                color: #0070d2;
                border-bottom: 2px solid #0070d2;
                padding-bottom: 8px;
                margin-bottom: 15px;
                font-size: 14pt;
            }
            
            .section h3 {
                color: #444;
                font-size: 12pt;
                margin-top: 20px;
                margin-bottom: 10px;
            }
            
            .info-box {
                background: #f8f9fa;
                padding: 15px;
                border-radius: 5px;
                margin-bottom: 15px;
            }
            
            .timeline-item {
                border-left: 3px solid #0070d2;
                padding-left: 15px;
                margin-bottom: 20px;
                position: relative;
            }
            
            .timeline-item::before {
                content: "";
                width: 10px;
                height: 10px;
                background: #0070d2;
                border-radius: 50%;
                position: absolute;
                left: -6px;
                top: 5px;
            }
            
            .timeline-author {
                font-weight: bold;
                color: #555;
                font-size: 10pt;
            }
            
            .timeline-date {
                font-size: 9pt;
                color: #888;
                margin-bottom: 8px;
            }
            
            .timeline-public {
                display: inline-block;
                padding: 2px 8px;
                border-radius: 3px;
                font-size: 8pt;
                font-weight: bold;
                margin-left: 10px;
            }
            
            .timeline-public.public {
                background: #d4edda;
                color: #155724;
            }
            
            .timeline-public.internal {
                background: #f8d7da;
                color: #721c24;
            }
            
            .screenshot-item {
                page-break-inside: avoid;
                margin-bottom: 25px;
                border: 1px solid #ddd;
                padding: 10px;
                border-radius: 5px;
            }
            
            .screenshot-item img {
                max-width: 100%;
                height: auto;
                display: block;
                margin: 10px 0;
            }
            
            .screenshot-description {
                font-size: 10pt;
                color: #666;
                font-style: italic;
            }
            
            .email-item {
                background: #f8f9fa;
                padding: 12px;
                border-radius: 5px;
                margin-bottom: 15px;
                border-left: 3px solid #6c757d;
            }
            
            .email-subject {
                font-weight: bold;
                color: #333;
            }
            
            .email-meta {
                font-size: 9pt;
                color: #666;
                margin-bottom: 8px;
            }
            
            .email-sentiment {
                display: inline-block;
                padding: 2px 8px;
                border-radius: 3px;
                font-size: 8pt;
                font-weight: bold;
                margin-left: 10px;
            }
            
            .sentiment-Positive { background: #d4edda; color: #155724; }
            .sentiment-Neutral { background: #e2e3e5; color: #383d41; }
            .sentiment-Frustrated { background: #fff3cd; color: #856404; }
            .sentiment-Urgent { background: #f8d7da; color: #721c24; }
            
            .evidence-item {
                page-break-inside: avoid;
                margin-bottom: 20px;
                border: 1px solid #ddd;
                padding: 10px;
                border-radius: 5px;
            }
            
            .evidence-item img {
                max-width: 100%;
                height: auto;
            }
            
            .no-data {
                color: #888;
                font-style: italic;
                padding: 20px;
                text-align: center;
                background: #f8f9fa;
                border-radius: 5px;
            }
            
            .footer {
                margin-top: 40px;
                border-top: 1px solid #ddd;
                padding-top: 15px;
                font-size: 9pt;
                color: #888;
                text-align: center;
            }
            
            table {
                width: 100%;
                border-collapse: collapse;
                margin-bottom: 15px;
            }
            
            th, td {
                padding: 8px 12px;
                text-align: left;
                border-bottom: 1px solid #ddd;
            }
            
            th {
                background: #f8f9fa;
                font-weight: bold;
                color: #333;
            }
        ''')
        
        pdf_bytes = html.write_pdf(stylesheets=[css])
        logger.info(f"Generated PDF ({len(pdf_bytes)} bytes) for {filename_hint}")
        return pdf_bytes
        
    except Exception as e:
        logger.error(f"WeasyPrint error generating PDF for {filename_hint}: {e}")
        return None


@transaction.atomic
def generate_response_letter(dispute_or_id):
    """
    Generate a professional dispute response letter using AI.

    Accepts either a Dispute instance (for testing/direct use) or a dispute_id
    integer (for production callers via frontend_views).

    Steps:
    1. Fetch Dispute with full Zendesk ticket data (custom fields, comments)
    2. Use AIClient with dispute_response_prompt from SystemSettings
       - Manager template stays in system role unchanged (no .format() interpolation)
       - Trusted dispute fields passed as structured text
       - Untrusted Zendesk ticket/comment data fenced in user role
    3. Save as DisputeDocument (type=RESPONSE_LETTER, status=DRAFT, generated_by=AI)
    4. Render to PDF via WeasyPrint
    5. Log generation to DisputeActivityLog

    Args:
        dispute_or_id: Dispute instance or integer primary key of the Dispute

    Returns:
        DisputeDocument instance on success (when called with int), or
        "<subject>\\n\\n<body>" string (when called with Dispute object), or
        None on failure.
    """
    # Support both dispute_id (int) and dispute object (for testing)
    if isinstance(dispute_or_id, int):
        dispute_id = dispute_or_id
        logger.info(f"Starting response letter generation for Dispute #{dispute_id}")
        try:
            dispute = Dispute.objects.select_related('claim').get(pk=dispute_id)
        except Dispute.DoesNotExist:
            logger.error(f"Dispute #{dispute_id} not found")
            return None
        return_document = True
    else:
        dispute = dispute_or_id
        dispute_id = getattr(dispute, 'pk', None) or getattr(dispute, 'id', 'unknown')
        logger.info(f"Starting response letter generation for Dispute #{dispute_id}")
        return_document = False

    try:
        # Fetch Zendesk ticket and comments using module-level imports (patchable in tests)
        ticket = fetch_zendesk_ticket_full(dispute.zd_ticket_id) or {}
        comments = fetch_zendesk_comments(dispute.zd_ticket_id)

        # Read alias from Zendesk custom field so tokenizer ALIAS-tags it
        alias = ""
        for cf in ticket.get('custom_fields', []):
            if cf.get('id') == 13606076120860:
                alias = cf.get('value') or ""
                break

        # Trusted: structured dispute fields (sourced from our own DB)
        trusted = {
            'dispute_reason': str(dispute.dispute_reason),
            'dispute_amount': str(dispute.dispute_amount),
            'buyer_name': str(dispute.buyer_name),
            'buyer_email': str(dispute.buyer_email),
            'transaction_id': str(dispute.transaction_id),
            'transaction_date': str(dispute.transaction_date),
            'zd_ticket_id': str(dispute.zd_ticket_id),
        }

        # Untrusted: Zendesk-sourced text — fenced by AIClient, never interpolated
        untrusted = {
            'ticket_subject': str(ticket.get('subject', ''))[:200],
            'ticket_description': str(ticket.get('description', ''))[:1000],
            'zendesk_comment': [str(c.get('body', ''))[:500] for c in comments[:5]],
        }

        # Get system prompt template from SystemSettings (passed as-is, no .format())
        system_settings = SystemSettings.get_instance()
        ai_prompt = system_settings.dispute_response_prompt

        subject, body = _call_qwen_ai(
            system_prompt=ai_prompt,
            trusted=trusted,
            untrusted=untrusted,
            known_aliases=[alias] if alias else [],
        )
        ai_generated_content = f"{subject}\n\n{body}"

        # When called with a dispute object (e.g. tests), return the text directly
        if not return_document:
            return ai_generated_content

        # Production path: render PDF and persist as DisputeDocument
        template_context = {
            'dispute': dispute,
            'ticket': ticket,
            'comments': comments,
            'ai_generated_content': ai_generated_content,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

        html_string = render_to_string('dispute_response_letter.html', template_context)
        pdf_bytes = _render_to_pdf(html_string, f"Dispute #{dispute_id} Response Letter")

        if not pdf_bytes:
            logger.error(f"Failed to generate PDF for Dispute #{dispute_id}")
            return None

        document = DisputeDocument.objects.create(
            dispute=dispute,
            doc_type='RESPONSE_LETTER',
            status='DRAFT',
            generated_by='AI',
            content_html=ai_generated_content,
            version=1,
        )

        from django.core.files.base import ContentFile
        filename = f"response_letter_dispute_{dispute_id}_v1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        document.file_path.save(filename, ContentFile(pdf_bytes), save=True)

        DisputeActivityLog.objects.create(
            dispute=dispute,
            action='DOCUMENT_GENERATED',
            details=f"AI-generated response letter (v1) created. Content length: {len(ai_generated_content)} chars, PDF size: {len(pdf_bytes)} bytes",
        )

        logger.info(f"Successfully generated response letter for Dispute #{dispute_id} (Document #{document.id})")
        return document

    except Exception as e:
        logger.error(f"Error generating response letter for Dispute #{dispute_id}: {e}")
        return None


@transaction.atomic
def generate_evidence_report(dispute_id: int) -> Optional[DisputeDocument]:
    """
    Generate a comprehensive evidence report for a dispute.
    
    This is a template-based (NO AI) structured factual report that compiles:
    - Ticket data
    - Screenshots
    - Claim evidence
    - Communication history
    
    Steps:
    1. Fetch Dispute + Zendesk ticket data
    2. Fetch all screenshots for the dispute
    3. Fetch claim evidence images
    4. Fetch communication history (emails)
    5. Render template-based report (structured, factual)
    6. Save as DisputeDocument (type=EVIDENCE_REPORT, status=DRAFT, generated_by=MANUAL)
    7. Render to PDF via WeasyPrint
    8. Log generation to DisputeActivityLog
    
    Args:
        dispute_id: Primary key of the Dispute
        
    Returns:
        DisputeDocument instance on success, None on failure
    """
    logger.info(f"Starting evidence report generation for Dispute #{dispute_id}")
    
    try:
        # Fetch the dispute
        dispute = Dispute.objects.select_related('claim').get(pk=dispute_id)
    except Dispute.DoesNotExist:
        logger.error(f"Dispute #{dispute_id} not found")
        return None
    
    try:
        # Fetch Zendesk ticket data
        zd_data = _fetch_zendesk_ticket_full(dispute.zd_ticket_id)
        ticket = zd_data.get('ticket', {})
        comments = zd_data.get('comments', [])
        
        # Fetch screenshots for the dispute
        screenshots = []
        for screenshot in DisputeScreenshot.objects.filter(dispute=dispute).order_by('-captured_at'):
            data_uri = _encode_screenshot_to_base64(screenshot)
            if data_uri:
                screenshots.append({
                    'id': screenshot.id,
                    'description': screenshot.description,
                    'page_url': screenshot.page_url,
                    'captured_at': screenshot.captured_at,
                    'data_uri': data_uri,
                })
        logger.info(f"Fetched {len(screenshots)} screenshots for Dispute #{dispute_id}")
        
        # Fetch claim evidence images
        evidence_list = []
        if dispute.claim:
            evidence_list = _fetch_claim_evidence_base64(dispute.claim)
        logger.info(f"Fetched {len(evidence_list)} claim evidence items for Dispute #{dispute_id}")
        
        # Fetch communication history (emails)
        communication_history = _fetch_communication_history(dispute)
        
        # Prepare template context
        template_context = {
            'dispute': dispute,
            'ticket': ticket,
            'comments': comments,
            'screenshots': screenshots,
            'claim_evidence': evidence_list,
            'communication_history': communication_history,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        
        # Render HTML template
        html_string = render_to_string('dispute_evidence_report.html', template_context)
        
        # Generate PDF
        pdf_bytes = _render_to_pdf(html_string, f"Dispute #{dispute_id} Evidence Report")
        
        if not pdf_bytes:
            logger.error(f"Failed to generate PDF for Dispute #{dispute_id}")
            return None
        
        # Create DisputeDocument record
        document = DisputeDocument.objects.create(
            dispute=dispute,
            doc_type='EVIDENCE_REPORT',
            status='DRAFT',
            generated_by='MANUAL',
            content_html=html_string,
            version=1,
        )
        
        # Save PDF file
        from django.core.files.base import ContentFile
        filename = f"evidence_report_dispute_{dispute_id}_v1_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        document.file_path.save(filename, ContentFile(pdf_bytes), save=True)
        
        # Log the activity
        DisputeActivityLog.objects.create(
            dispute=dispute,
            action='DOCUMENT_GENERATED',
            details=f"Evidence report (v1) created. Screenshots: {len(screenshots)}, Evidence: {len(evidence_list)}, Emails: {len(communication_history)}, PDF size: {len(pdf_bytes)} bytes",
        )
        
        logger.info(f"Successfully generated evidence report for Dispute #{dispute_id} (Document #{document.id})")
        return document
        
    except Exception as e:
        logger.error(f"Error generating evidence report for Dispute #{dispute_id}: {e}")
        return None


def regenerate_document(document_id: int) -> Optional[DisputeDocument]:
    """
    Regenerate an existing document (increment version).
    
    Args:
        document_id: Primary key of the DisputeDocument
        
    Returns:
        New DisputeDocument instance on success, None on failure
    """
    try:
        old_document = DisputeDocument.objects.get(pk=document_id)
        dispute = old_document.dispute
        
        if old_document.doc_type == 'RESPONSE_LETTER':
            new_document = generate_response_letter(dispute.id)
        else:
            new_document = generate_evidence_report(dispute.id)
        
        if new_document:
            # Increment version
            new_document.version = old_document.version + 1
            new_document.save(update_fields=['version'])
            
            # Log the regeneration
            DisputeActivityLog.objects.create(
                dispute=dispute,
                action='DOCUMENT_GENERATED',
                details=f"Document regenerated (v{new_document.version}) from original v{old_document.version}",
            )
        
        return new_document
        
    except DisputeDocument.DoesNotExist:
        logger.error(f"Document #{document_id} not found")
        return None
    except Exception as e:
        logger.error(f"Error regenerating document #{document_id}: {e}")
        return None
