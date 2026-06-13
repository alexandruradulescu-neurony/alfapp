"""
Document Generation Service for LORA Dispute Management.

Generates professional dispute response letters and evidence reports as PDF documents.
Uses Qwen AI for response letter generation and template-based rendering for evidence reports.
"""

import base64
import logging
import os
import re
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
                    'category': email_log.category,
                    'category_display': email_log.get_category_display(),
                    'ai_summary': email_log.ai_summary,
                    'auto_resolved': email_log.auto_resolved,
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


# Category → evidence-report template. Each dispute category can have its own
# layout; until the per-category report models are provided they all use the
# generic Zendesk-styled report. Register a new template here (and add the
# file) when a category's model arrives — no other code changes needed.
# The narrative report (matches the business's Word template) is the default.
# The old metadata-dump template stays available under its name for fallback.
GENERIC_EVIDENCE_TEMPLATE = 'disputes/narrative_evidence_report.html'
LEGACY_EVIDENCE_TEMPLATE = 'dispute_evidence_report.html'
CATEGORY_REPORT_TEMPLATES = {
    # Per-category layouts can override here; by default every category uses the
    # narrative template and only its framing text (CATEGORY_FRAMING) changes.
}

# Fixed, case-independent images shipped with the report (canonical screenshots
# of the public site + the annotated checkout flow). Lifted from the business's
# own Word template so reports look identical to what the team sends today.
REPORT_ASSETS_DIR = os.path.join(os.path.dirname(__file__), 'report_assets')
TERMS_URL = 'https://airportlostfound.com/legal/terms-and-conditions/'

# Per-category framing: the headline + lead paragraph that opens the evidence,
# reordered to rebut the specific claim. Same evidence underneath.
CATEGORY_FRAMING = {
    'UNAUTHORISED': {
        'headline': 'Evidence the customer authorised this transaction',
        'lead': ('The buyer states the transaction was not authorised. The records below show '
                 'the customer personally submitted this claim on our website, accepted our Terms '
                 'and Conditions, and expressly authorised us to act on their behalf — after which '
                 'we performed the search service they paid for.'),
    },
    'MERCHANDISE_OR_SERVICE_NOT_RECEIVED': {
        'headline': 'Evidence the service was delivered',
        'lead': ('The buyer states the service was not received. The records below show the search '
                 'service began immediately after purchase: the lost item was reported to the airline '
                 'and airport, and the customer was kept updated throughout.'),
    },
    'MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED': {
        'headline': 'Evidence the service matched what was offered',
        'lead': ('The buyer states the service was not as described. The records below show exactly '
                 'what was offered at checkout and that the service performed matched that description.'),
    },
    'CREDIT_NOT_PROCESSED': {
        'headline': 'Evidence of the refund policy the customer accepted',
        'lead': ('The buyer expected a credit. The records below show the refund policy the customer '
                 'accepted at checkout and the search service that was performed under it.'),
    },
}
DEFAULT_FRAMING = {
    'headline': 'Evidence of service delivered and authorisation given',
    'lead': ("The records below document the customer's purchase, the authorisation they gave us, and "
             'the search service we performed on their behalf.'),
}


def _asset_data_uri(filename: str) -> Optional[str]:
    """Base64 data URI for a fixed report asset, or None if missing."""
    try:
        path = os.path.join(REPORT_ASSETS_DIR, filename)
        with open(path, 'rb') as f:
            data = f.read()
        ext = filename.rsplit('.', 1)[-1].lower()
        mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                'gif': 'image/gif', 'webp': 'image/webp'}.get(ext, 'image/jpeg')
        return f"data:{mime};base64,{base64.b64encode(data).decode('utf-8')}"
    except Exception as e:
        logger.warning(f"Report asset {filename} unavailable: {e}")
        return None


def _attachment_data_uri(content_type: str, content_url: str) -> Optional[str]:
    """Download an image attachment from Zendesk and return it as a data URI.
    Non-images and failures return None (so the template just skips them)."""
    if not (content_type or '').lower().startswith('image/'):
        return None
    from apps.integrations.services import fetch_zendesk_attachment_bytes
    data = fetch_zendesk_attachment_bytes(content_url)
    if not data:
        return None
    return f"data:{content_type};base64,{base64.b64encode(data).decode('utf-8')}"


def _zendesk_comment_panels(comments: list, embed_images: bool = True,
                            max_images: int = 14) -> list:
    """Turn Zendesk comments into 'simulated screenshot' panels: author,
    public/internal flag, timestamp, body text, and embedded attachment images
    (the pasted airline confirmations, lost-&-found forms, etc.)."""
    panels = []
    embedded = 0
    for c in comments:
        images = []
        if embed_images:
            for att in c.get('attachments', []):
                if embedded >= max_images:
                    break
                uri = _attachment_data_uri(att.get('content_type', ''), att.get('content_url', ''))
                if uri:
                    images.append({'data_uri': uri, 'file_name': att.get('file_name', '')})
                    embedded += 1
        author = c.get('author', {}) or {}
        public = c.get('public', False)
        name = author.get('name') or ''
        if not name or name == 'Unknown':
            name = 'Support agent' if public else 'Airport Lost & Found team'
        panels.append({
            'author': name,
            'author_email': author.get('email', ''),
            'public': public,
            'created_at': _fmt_zd_time(c.get('created_at')),
            'body': _clean_comment_body(c.get('body', '')),
            'images': images,
        })
    return panels


# Marks the start of LORA's internal email-processing trailer that some notes
# carry ("**AI Analysis** / Category: / Action Required: / Auto-Resolved:").
# That is internal automation metadata and must never face PayPal — strip it.
_AI_TRAILER_RE = re.compile(r'\*{0,2}\s*AI Analysis', re.IGNORECASE)
_HR_LINE_RE = re.compile(r'(?m)^\s*-{3,}\s*$')
_ENVELOPE_RE = re.compile(r'[\U0001F4E7\U0001F4E8\U0001F4E9✉️]')


def _clean_comment_body(body: str) -> str:
    """Make a Zendesk comment body presentable in a client-facing PDF: drop the
    internal AI-analysis trailer, markdown bold/HR markers, and envelope icons."""
    if not body:
        return ''
    text = _AI_TRAILER_RE.split(body, maxsplit=1)[0]
    text = text.replace('**', '').replace('__', '')
    text = _HR_LINE_RE.sub('', text)
    text = _ENVELOPE_RE.sub('', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _fmt_zd_time(value) -> str:
    """Format a Zendesk ISO timestamp ('2026-06-11T07:30:46Z') as 'Jun 11, 2026 07:30'."""
    if not value:
        return ''
    if not isinstance(value, str):
        try:
            return value.strftime('%b %d, %Y %H:%M')
        except Exception:
            return str(value)
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).strftime('%b %d, %Y %H:%M')
    except Exception:
        return value


def _flight_card(claim) -> Optional[dict]:
    """Compact flight-card context rebuilt from the claim's stored flight
    lookup (claim.flight_data) — rendered natively, not screenshotted."""
    fd = getattr(claim, 'flight_data', None) or {}
    legs = fd.get('legs') or []
    if not legs:
        return None
    first, last = legs[0], legs[-1]
    return {
        'number': fd.get('number', ''),
        'airline': fd.get('airline', ''),
        'status': fd.get('status', '') or first.get('status', ''),
        'from_iata': first.get('from_iata', ''),
        'from_city': first.get('from_city', '') or first.get('from_name', ''),
        'to_iata': last.get('to_iata', ''),
        'to_city': last.get('to_city', '') or last.get('to_name', ''),
        'dep_local': first.get('scheduled_departure_local', ''),
        'arr_local': last.get('scheduled_arrival_local', ''),
        'from_terminal': first.get('from_terminal', ''),
        'from_gate': first.get('from_gate', ''),
        'to_terminal': last.get('to_terminal', ''),
        'to_gate': last.get('to_gate', ''),
    }


def _narrative_fields(dispute) -> dict:
    """The header/narrative values the template slots into the Word-template
    prose. Missing pieces are left blank — never fabricated."""
    claim = dispute.claim
    fee = None
    if claim and claim.price_paid is not None:
        fee = claim.price_paid
    elif dispute.dispute_amount is not None:
        fee = dispute.dispute_amount
    currency = dispute.dispute_currency or 'USD'
    object_short = ''
    if claim and claim.object_description:
        object_short = claim.object_description.strip().splitlines()[0][:120]
    return {
        'client_name': (claim.client_name if claim else '') or dispute.buyer_name or 'the customer',
        'alf_id': (claim.alf_claim_id if claim else '') or '',
        'object': object_short,
        'fee': fee,
        'currency': currency,
        'visit_date': (claim.created_at if claim else dispute.transaction_date),
        'terms_url': TERMS_URL,
    }


# Narrative sections, in the order the report presents them (mirrors the
# business's Word template). 'OTHER' catches anything the AI can't place.
SECTION_ORDER = [
    ('SERVICE_INITIATION', 'Service initiation'),
    ('FLIGHT_IDENTIFICATION', 'Flight identification'),
    ('INTERACTIONS', 'Interactions with the client'),
    ('SUBMISSIONS', 'Submission of claims to lost & found offices'),
    ('CLAIM_UPDATES', 'Claim updates'),
    ('OTHER', 'Additional case records'),
]

EVIDENCE_NARRATIVE_SYSTEM_PROMPT = (
    "You organise evidence for a PayPal dispute defence on behalf of a paid "
    "lost-item recovery service (the merchant). You are given numbered evidence "
    "records taken from the merchant's support system for one case. For EACH "
    "record, decide:\n"
    "1. section — the best fit from: SERVICE_INITIATION (the customer's own "
    "claim submission / intake), FLIGHT_IDENTIFICATION (verifying the flight), "
    "INTERACTIONS (the merchant contacting the customer by phone/email), "
    "SUBMISSIONS (reporting the lost item to airline/airport lost-&-found "
    "offices), CLAIM_UPDATES (status updates, item-found / return options), "
    "OTHER (relevant but uncategorised), or EXCLUDE.\n"
    "Use EXCLUDE for internal automation logs, system noise (e.g. abandoned-cart "
    "notices), duplicates, or anything that does NOT help the merchant's defence.\n"
    "2. explanation — ONE concise sentence stating what the record shows and why "
    "it supports the merchant (that the customer authorised the service and that "
    "the service was performed). Base it ONLY on the record text; never invent "
    "facts. Neutral, factual tone.\n"
    "Return JSON: {\"items\": [{\"index\": <int>, \"section\": <enum>, "
    "\"explanation\": <str>}, ...]} with one entry per record."
)


def _known_pii_for(claim) -> dict:
    """PII strings to force-mask before the LLM sees any evidence text — the
    client's name and addresses (not regex-detectable) plus alias/email/phone."""
    if not claim:
        return {}
    names = [getattr(claim, 'client_name', ''),
             getattr(claim, 'billing_address', ''),
             getattr(claim, 'shipping_address', '')]
    aliases = [getattr(claim, 'email_alias', ''),
               getattr(claim, 'client_email', ''),
               getattr(claim, 'alternate_email', ''),
               getattr(claim, 'phone', '') or '']
    return {
        'names': [str(n).strip() for n in names if n and str(n).strip()],
        'aliases': [str(a).strip() for a in aliases if a and str(a).strip()],
    }


def _narrate_evidence(dispute, items: list, claim) -> Optional[dict]:
    """Ask the AI to sort each evidence record into a narrative section and
    write a one-line relevance note. Returns {index: {'section','explanation'}},
    or None on any failure / when AI is not configured (caller falls back to an
    ungrouped list). PII is force-masked via known_pii before the LLM sees text."""
    if not items:
        return None
    try:
        ss = SystemSettings.get_instance()
        if not getattr(ss, 'ai_api_key', ''):
            return None  # AI not configured — skip the call entirely (tests, etc.)
        from apps.ai.client import AIClient
        from apps.ai.schemas import EvidenceNarrative
    except Exception:
        return None

    trusted = {'dispute_reason': dispute.dispute_reason or 'uncategorised'}
    # Fence the records under the approved 'zendesk_comment' tag (prompt_fence
    # ALLOWED_TAGS); the [index] prefix inside each tells the AI which record.
    untrusted = {'zendesk_comment': [
        f"[{it['index']}] ({'public reply' if it['channel'] == 'public' else 'internal note'}"
        f"{', has image' if it.get('has_image') else ''}): {it['text']}"
        for it in items
    ]}
    try:
        result = AIClient.complete(
            system_prompt=EVIDENCE_NARRATIVE_SYSTEM_PROMPT,
            trusted=trusted,
            untrusted=untrusted,
            known_pii=_known_pii_for(claim),
            response_schema=EvidenceNarrative,
            call_site='dispute_evidence_narrative',
            temperature=0.2,
            max_tokens=900,
        )
    except Exception as e:
        logger.warning(f"Evidence narrative AI unavailable; using ungrouped fallback: {e}")
        return None
    return {p.index: {'section': p.section, 'explanation': p.explanation} for p in result.items}


def _item_entry(item: dict, explanation: str = '') -> dict:
    """One rendered evidence entry (a panel or the flight card) + its note."""
    entry = {'explanation': explanation}
    if item['kind'] == 'flight_card':
        entry['flight_card'] = item['flight_card']
    else:
        entry['panel'] = item['panel']
    return entry


def _group_into_sections(items: list, narrative: Optional[dict]) -> list:
    """Group evidence items into ordered narrative sections. With a narrative
    mapping, place/caption/exclude per the AI; without one, return a single
    ungrouped 'Case record' section (graceful fallback)."""
    if not items:
        return []
    if not narrative:
        return [{'key': 'ALL', 'title': 'Case record',
                 'items': [_item_entry(it) for it in items]}]

    buckets = {key: [] for key, _ in SECTION_ORDER}
    for it in items:
        placement = narrative.get(it['index']) or {}
        section = placement.get('section') or 'OTHER'
        if section == 'EXCLUDE':
            continue
        if section not in buckets:
            section = 'OTHER'
        buckets[section].append(_item_entry(it, placement.get('explanation', '')))
    return [{'key': key, 'title': title, 'items': buckets[key]}
            for key, title in SECTION_ORDER if buckets[key]]


def report_template_for(dispute) -> str:
    """The evidence-report template for this dispute's category (Phase 5)."""
    return CATEGORY_REPORT_TEMPLATES.get(dispute.dispute_reason, GENERIC_EVIDENCE_TEMPLATE)


def build_dispute_evidence_bundle(dispute, embed_attachments: bool = True,
                                  use_ai: bool = True) -> dict:
    """Gather EVERYTHING an evidence report could need for a dispute, once,
    into a structured context — independent of how any report lays it out.

    The case records (Zendesk comments + the flight card) are sorted by the AI
    into ordered narrative `sections`, each item carrying a one-line relevance
    note. When AI is unavailable/disabled they collapse into a single ungrouped
    section. Also includes captured/uploaded screenshots, claim evidence images,
    the email history, the fixed report assets, and category framing.
    """
    zd_data = _fetch_zendesk_ticket_full(dispute.zd_ticket_id)
    ticket = zd_data.get('ticket', {})
    comments = zd_data.get('comments', [])

    screenshots = []
    # A transient (unsaved) dispute — used by the --zd-ticket preview — has no
    # pk, so it can't have attached screenshots; skip the related lookup.
    screenshot_qs = (DisputeScreenshot.objects.filter(dispute=dispute).order_by('-captured_at')
                     if dispute.pk else DisputeScreenshot.objects.none())
    for screenshot in screenshot_qs:
        data_uri = _encode_screenshot_to_base64(screenshot)
        if data_uri:
            screenshots.append({
                'id': screenshot.id,
                'description': screenshot.description,
                'page_url': screenshot.page_url,
                'captured_at': screenshot.captured_at,
                'data_uri': data_uri,
            })

    evidence_list = _fetch_claim_evidence_base64(dispute.claim) if dispute.claim else []
    communication_history = _fetch_communication_history(dispute)
    panels = _zendesk_comment_panels(comments, embed_images=embed_attachments)
    flight_card = _flight_card(dispute.claim)
    framing = CATEGORY_FRAMING.get(dispute.dispute_reason, DEFAULT_FRAMING)

    # Assemble the evidence items the report can show, then let the AI sort them
    # into narrative sections with relevance notes (or fall back to ungrouped).
    items = []
    if flight_card:
        items.append({
            'index': 0, 'kind': 'flight_card', 'channel': 'internal', 'has_image': False,
            'text': (f"Independent airline-data flight record: {flight_card['airline']} "
                     f"{flight_card['number']}, {flight_card['from_iata']}→{flight_card['to_iata']}, "
                     f"status {flight_card['status']}."),
            'flight_card': flight_card,
        })
    for i, p in enumerate(panels, start=1):
        items.append({
            'index': i, 'kind': 'comment',
            'channel': 'public' if p['public'] else 'internal',
            'has_image': bool(p['images']), 'text': (p['body'] or '')[:700], 'panel': p,
        })

    narrative = _narrate_evidence(dispute, items, dispute.claim) if use_ai else None
    sections = _group_into_sections(items, narrative)

    return {
        'dispute': dispute,
        'claim': dispute.claim,
        'ticket': ticket,
        'comments': comments,
        'panels': panels,
        'flight_card': flight_card,
        'sections': sections,
        'narrative': _narrative_fields(dispute),
        'framing': framing,
        'assets': {
            'homepage': _asset_data_uri('homepage.jpg'),
            'checkout': _asset_data_uri('checkout_annotated.jpg'),
        },
        'screenshots': screenshots,
        'claim_evidence': evidence_list,
        'communication_history': communication_history,
        'category': dispute.dispute_reason,
        'category_label': dispute.get_dispute_reason_display() if dispute.dispute_reason else '',
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


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
        # Assemble the full evidence bundle (report-independent) and render the
        # category's report template (Phase 5).
        template_context = build_dispute_evidence_bundle(dispute)
        screenshots = template_context['screenshots']
        evidence_list = template_context['claim_evidence']
        communication_history = template_context['communication_history']
        logger.info(
            f"Bundle for Dispute #{dispute_id}: {len(screenshots)} screenshots, "
            f"{len(evidence_list)} evidence, {len(communication_history)} emails")

        html_string = render_to_string(report_template_for(dispute), template_context)

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
