"""
Document Generation Service for LORA Dispute Management.

Generates professional dispute response letters and evidence reports as PDF documents.
Uses Qwen AI for response letter generation and template-based rendering for evidence reports.
"""

import base64
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone as _std_timezone
from typing import Optional, Tuple

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from django.template.loader import render_to_string
from django.utils import timezone as dj_timezone
from apps.payments.models import Dispute, DisputeDocument, DisputeActivityLog
from apps.config.models import SystemSettings
from apps.communications.models import EmailLog
from apps.claims.models import ClaimEvidence

logger = logging.getLogger(__name__)


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


def _fetch_zendesk_ticket_full(zd_ticket_id: str) -> dict:
    """
    Fetch complete Zendesk ticket data including custom fields and comments.
    
    Args:
        zd_ticket_id: The Zendesk ticket ID
        
    Returns:
        Dictionary with ticket data and comments, or empty dict on failure
    """
    # Imported inside the function so tests can patch
    # apps.integrations.services.* at call time (see test_document_screenshot_services).
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




def _sniff_image_mime(data: bytes, fallback: str = 'image/jpeg') -> str:
    """MIME from the actual image bytes (Pillow), NOT the spoofable filename
    extension — a mislabeled upload then embeds with its real type. Falls back
    when Pillow is missing or the bytes aren't a recognised image."""
    try:
        import io
        from PIL import Image
        fmt = (Image.open(io.BytesIO(data)).format or '').upper()
        return _PIL_MIME.get(fmt, fallback)
    except Exception:
        return fallback


def _evidence_within_media_root(evidence) -> bool:
    """Path-traversal guard for filesystem storage (commonpath, not startswith,
    so a sibling like '/srv/media_evil' can't pass for MEDIA_ROOT '/srv/media').
    Remote backends (no local .path) skip the check and rely on the storage API."""
    try:
        abs_path = os.path.abspath(evidence.image.path)
    except NotImplementedError:
        return True
    except (SuspiciousFileOperation, ValueError) as e:
        logger.warning(f"Evidence file path rejected as outside MEDIA_ROOT: {e}")
        return False
    media_root = os.path.abspath(settings.MEDIA_ROOT)
    try:
        if os.path.commonpath([abs_path, media_root]) != media_root:
            logger.warning(f"Evidence file path outside MEDIA_ROOT: {abs_path}")
            return False
    except ValueError:
        return False
    return True


def _fetch_claim_evidence_base64(claim) -> list:
    """
    Fetch all claim evidence images and encode as base64 data URIs.

    MIME is sniffed from the actual bytes (Pillow), not the filename extension.
    Path-guarded to MEDIA_ROOT; file handles are always closed.

    Args:
        claim: Claim instance

    Returns:
        List of dicts with description, uploaded_at, and data_uri
    """
    evidence_list = []

    for evidence in claim.evidence.all().iterator():
        if not evidence.image:
            continue
        if not _evidence_within_media_root(evidence):
            continue
        try:
            evidence.image.open('rb')
            try:
                image_data = evidence.image.read()
            finally:
                evidence.image.close()
            image_base64 = base64.b64encode(image_data).decode('utf-8')
            mime_type = _sniff_image_mime(image_data)
            evidence_list.append({
                'description': evidence.description,
                'uploaded_at': evidence.uploaded_at,
                'data_uri': f"data:{mime_type};base64,{image_base64}",
            })
        except Exception as e:
            logger.warning(f"Error processing evidence {evidence.id}: {e}")
            continue

    return evidence_list


def _fetch_communication_history(dispute: Dispute, claim_emails: Optional[list] = None) -> list:
    """
    Fetch communication history (emails) related to the dispute's claim.

    Pass `claim_emails` (a prefetched list of EmailLog rows for the claim) to
    avoid re-querying — build_dispute_evidence_bundle loads them once and shares
    them with this and _identity_context.

    Args:
        dispute: Dispute instance
        claim_emails: optional prefetched EmailLog list for the dispute's claim

    Returns:
        List of email log entries (latest 50, newest first)
    """
    emails = []

    if dispute.claim:
        try:
            if claim_emails is None:
                rows = list(EmailLog.objects.filter(claim=dispute.claim)
                            .order_by('-received_at')[:50])
            else:
                # Newest first, capped at 50 — sorted in Python off the shared list.
                rows = sorted(claim_emails, key=lambda e: e.received_at, reverse=True)[:50]
            for email_log in rows:
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


def _persist_document(dispute, *, doc_type: str, generated_by: str, content_html: str,
                      pdf_bytes: bytes, details: str) -> DisputeDocument:
    """Create an auto-versioned DisputeDocument, save its PDF, and write exactly
    ONE DOCUMENT_GENERATED activity-log line — all in a narrow transaction.

    The version is computed as max(existing for this dispute+doc_type) + 1, and
    the SAME version flows into the filename, the version field, and the log line,
    so a regenerated v2 is never mislabelled v1 and only one log entry is emitted
    (fixes the old hardcoded version=1 / _v1_ filename / double-log bug). Used by
    generate_evidence_report (the only generator now that the response letter is
    gone) — keeps the create+file-save+log block in one place."""
    slug = 'evidence_report'
    with transaction.atomic():
        # Lock the dispute row so two concurrent generations serialise and take
        # distinct versions instead of both landing on the same one.
        Dispute.objects.select_for_update().filter(pk=dispute.pk).first()
        version = (DisputeDocument.objects
                   .filter(dispute=dispute, doc_type=doc_type)
                   .aggregate(m=Max('version'))['m'] or 0) + 1
        filename = (f"{slug}_dispute_{dispute.pk}_v{version}_"
                    f"{dj_timezone.now().strftime('%Y%m%d_%H%M%S')}.pdf")
        document = DisputeDocument.objects.create(
            dispute=dispute,
            doc_type=doc_type,
            status=DisputeDocument.STATUS_DRAFT,
            generated_by=generated_by,
            content_html=content_html,
            version=version,
        )
        document.file_path.save(filename, ContentFile(pdf_bytes), save=True)
        DisputeActivityLog.objects.create(
            dispute=dispute,
            action=DisputeActivityLog.ACTION_DOCUMENT_GENERATED,
            details=f"{details} (v{version})",
        )
    return document


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
# PayPal evidence uploads are typically capped near 10MB; warn before that.
PAYPAL_EVIDENCE_SIZE_WARN_BYTES = 9_500_000

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


# Comment images (attachments + inline-pasted) are fetched up to this ceiling
# then DOWNSCALED before embedding, so a big screenshot still appears (the old
# 5 MB fetch cap silently dropped large Delta/Denver-style screenshots) while the
# PDF stays bounded. Images already small + in-bounds pass through untouched.
_IMG_FETCH_MAX_BYTES = 25_000_000
_IMG_PASSTHROUGH_BYTES = 1_500_000
_IMG_EMBED_MAX_DIM = 1600
_IMG_EMBED_QUALITY = 85
# Below this (px, both sides) an image is a tracking pixel / signature icon /
# emoji, not evidence — skip it (esp. images riding along in ingested-email
# comments) so the report doesn't fill with junk.
_IMG_MIN_DIM = 64
_PIL_MIME = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'JPG': 'image/jpeg',
             'GIF': 'image/gif', 'WEBP': 'image/webp'}

# Display/log truncation caps — how much free text we keep when rendering a
# value into the report (or feeding it to the narrative AI). Cosmetic bounds
# only; they never change which records are shown, just how much of each.
_LOST_LOCATION_DISPLAY_CHARS = 200    # claim's lost-location line in the facts block
_CASE_LOG_TEXT_DISPLAY_CHARS = 600    # notes/message text in a case-log entry
_EVIDENCE_RECORD_TEXT_CHARS = 700     # comment body length sent to the narrative AI


# Zendesk blocks the default "Python-urllib/x" user-agent (403); any non-bot UA
# is accepted. Verified live against /attachments/token/ URLs.
_IMG_USER_AGENT = 'Mozilla/5.0 (compatible; LORA-evidence/1.0)'


def _image_host_allowed(host: str) -> bool:
    """True only for hosts we'll fetch dispute-evidence images from: our own
    Zendesk subdomain or Zendesk's signed content CDN. Applied to every redirect
    hop and the final landing host, so an allowlisted URL can't bounce the fetch
    to an internal/foreign address (SSRF). Mirrors the host policy in
    _fetch_zendesk_image_bytes."""
    host = (host or '').lower()
    if not host:
        return False
    sub = (SystemSettings.get_instance().zd_subdomain or '').strip().lower()
    return (
        (bool(sub) and host == f"{sub}.zendesk.com")
        or host.endswith('.zdusercontent.com')
        or host.endswith('.zendeskusercontent.com')
    )


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to FOLLOW a redirect whose target host is off-allowlist — an
    allowlisted Zendesk URL must not redirect the fetch to an internal/foreign
    address. Returning None aborts the redirect before any request is made to
    the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlparse
        if not _image_host_allowed(urlparse(newurl).hostname or ''):
            logger.warning("Refusing image-fetch redirect to off-allowlist host: %s",
                           newurl[:120])
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_no_auth(url: str, max_bytes: int) -> Optional[bytes]:
    """GET an image URL with NO Zendesk auth but a browser User-Agent.

    Zendesk's inline attachment token URLs (``/attachments/token/{token}/?name=``)
    REJECT Basic auth (403) and block the default python UA (403); the token in
    the path IS the credential, and the request 302-redirects to the signed
    content CDN (``*.zdusercontent.com``). Also used for the CDN host directly.
    Sending our Basic-auth creds here would be both wrong and a credential leak.

    Redirects are followed ONLY to allowlisted hosts (_AllowlistRedirectHandler),
    and the host we actually land on is re-checked before the body is read — so a
    redirect can't be used to reach an internal/foreign address (SSRF).
    """
    from urllib.parse import urlparse
    try:
        req = urllib.request.Request(url, method='GET', headers={'User-Agent': _IMG_USER_AGENT})
        timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
        opener = urllib.request.build_opener(_AllowlistRedirectHandler())
        with opener.open(req, timeout=timeout) as resp:
            # Defense in depth: even with the redirect handler, re-check the host
            # we actually landed on before trusting any bytes.
            final_host = (urlparse(resp.geturl()).hostname or '').lower()
            if not _image_host_allowed(final_host):
                logger.warning("Image fetch landed on off-allowlist host %s; refusing: %s",
                               final_host, url[:120])
                return None
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            logger.warning(f"Image exceeds {max_bytes} bytes; skipping embed: {url[:120]}")
            return None
        return data
    except Exception as e:
        logger.warning(f"Could not download image {url[:120]}: {e}")
        return None


def _fetch_zendesk_image_bytes(url: str) -> Optional[bytes]:
    """Fetch an image referenced from a Zendesk comment — an attachment
    content_url OR an inline-pasted body image (``![](url)``) — as raw bytes.

    SECURITY host policy (our Zendesk auth token must never reach a foreign host):
      * our own ``<sub>.zendesk.com`` → authenticated GET;
      * a relative ``/attachments/...`` url → resolved against our subdomain;
      * Zendesk's signed content CDN (``*.zdusercontent.com`` /
        ``*.zendeskusercontent.com``) → fetched WITHOUT auth (already pre-signed);
      * anything else → refused.
    Logs the reason whenever it returns None so a silent drop is diagnosable.
    """
    if not url:
        return None
    from urllib.parse import urlparse
    sub = (SystemSettings.get_instance().zd_subdomain or '').strip().lower()
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()

    if not host:  # relative url (e.g. "/attachments/token/...") → our host
        if not sub:
            logger.warning("Inline image skipped: relative url but zd_subdomain is unset")
            return None
        url = f"https://{sub}.zendesk.com{url if url.startswith('/') else '/' + url}"
        host = f"{sub}.zendesk.com"

    if sub and host == f"{sub}.zendesk.com":
        # These attachment token URLs reject Basic auth + the default UA (403).
        # No-auth + browser UA works (redirects to the signed CDN); fall back to
        # the authed API fetch only if that somehow returns nothing.
        data = _fetch_no_auth(url, _IMG_FETCH_MAX_BYTES)
        if not data:
            from apps.integrations.services import fetch_zendesk_attachment_bytes
            data = fetch_zendesk_attachment_bytes(url, max_bytes=_IMG_FETCH_MAX_BYTES)
        if not data:
            logger.warning(f"Zendesk image fetch failed (no-auth + authed): {url[:120]}")
        return data

    if host.endswith('.zdusercontent.com') or host.endswith('.zendeskusercontent.com'):
        return _fetch_no_auth(url, _IMG_FETCH_MAX_BYTES)

    logger.warning(f"Inline image skipped: host '{host}' is not our Zendesk host")
    return None


def _downscale_for_embed(data: bytes) -> Tuple[bytes, str]:
    """Return (bytes, mime) for embedding: small in-bounds images pass through
    untouched (lossless — keeps screenshot text crisp); oversized ones are
    resized to fit and recompressed so they EMBED instead of being dropped, and
    the PDF stays bounded. Falls back to the original bytes if Pillow is missing
    or the data isn't a decodable image."""
    try:
        import io
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        fmt = (img.format or '').upper()
        if max(img.size) < _IMG_MIN_DIM:
            return b'', ''   # tracking pixel / icon — not evidence, skip
        if (len(data) <= _IMG_PASSTHROUGH_BYTES
                and max(img.size) <= _IMG_EMBED_MAX_DIM and fmt in _PIL_MIME):
            return data, _PIL_MIME[fmt]
        img = ImageOps.exif_transpose(img)
        has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
        if max(img.size) > _IMG_EMBED_MAX_DIM:
            img.thumbnail((_IMG_EMBED_MAX_DIM, _IMG_EMBED_MAX_DIM))
        out = io.BytesIO()
        if has_alpha:
            img.convert('RGBA').save(out, format='PNG', optimize=True)
            return out.getvalue(), 'image/png'
        img.convert('RGB').save(out, format='JPEG', quality=_IMG_EMBED_QUALITY, optimize=True)
        return out.getvalue(), 'image/jpeg'
    except Exception as e:
        logger.warning(f"Image downscale skipped ({e}); embedding original bytes")
        return data, 'image/jpeg'


def _embed_image_data_uri(data: Optional[bytes]) -> Optional[str]:
    """Downscale + base64 image bytes into a ``data:`` URI for the PDF; None if
    there are no bytes."""
    if not data:
        return None
    out, mime = _downscale_for_embed(data)
    if not out:
        return None   # skipped (e.g. tracking pixel / icon)
    return f"data:{mime};base64,{base64.b64encode(out).decode('utf-8')}"


def _attachment_data_uri(content_type: str, content_url: str) -> Optional[str]:
    """Download an image attachment from Zendesk and return it as a data URI.
    Non-images and failures return None (so the template just skips them); large
    images are downscaled (not dropped) before embedding."""
    if not (content_type or '').lower().startswith('image/'):
        return None
    return _embed_image_data_uri(_fetch_zendesk_image_bytes(content_url))


def _inline_image_data_uri(url: str) -> Optional[str]:
    """Embed an image pasted INLINE into a Zendesk comment body (markdown
    ``![](url)``) as a data URI. Host/auth policy + downscaling are handled by
    _fetch_zendesk_image_bytes / _embed_image_data_uri."""
    return _embed_image_data_uri(_fetch_zendesk_image_bytes(url))


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
            # Images pasted INLINE don't appear in `attachments`. When an agent
            # pastes a screenshot in Zendesk's editor it lands in html_body as
            # <img src=...> and is usually ABSENT from the plain body (which keeps
            # only the typed label like "DELTA"); markdown ![](url) shows up only
            # for API/markdown-authored comments. Scan BOTH so neither is missed.
            for url in _comment_inline_image_urls(c):
                if embedded >= max_images:
                    break
                uri = _inline_image_data_uri(url)
                if uri:
                    images.append({'data_uri': uri, 'file_name': ''})
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
# Markdown image: ![alt](url). Group 1 is the URL. Inline images are embedded as
# real pictures by _zendesk_comment_panels, so the raw markdown is stripped here.
_MD_IMAGE_RE = re.compile(r'!\[[^\]]*\]\(([^)]+)\)')
# <img src="..."> in a comment's html_body — how Zendesk represents an image
# pasted into the agent editor (the plain body keeps only the typed text).
_HTML_IMG_RE = re.compile(r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)


def _comment_inline_image_urls(comment: dict) -> list:
    """Inline-pasted image URLs for a comment, from BOTH the plain-body markdown
    (``![](url)`` — API/markdown comments) and the html_body ``<img src>`` (agent
    paste). De-duped, order preserved. Host/auth filtering and the
    tracking-pixel/icon size cut happen downstream at fetch/embed time."""
    urls = list(_MD_IMAGE_RE.findall(comment.get('body', '') or ''))
    urls += list(_HTML_IMG_RE.findall(comment.get('html_body', '') or ''))
    seen, out = set(), []
    for u in urls:
        u = (u or '').strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _clean_comment_body(body: str) -> str:
    """Make a Zendesk comment body presentable in a client-facing PDF: drop the
    internal AI-analysis trailer, markdown bold/HR markers, and envelope icons."""
    if not body:
        return ''
    text = _AI_TRAILER_RE.split(body, maxsplit=1)[0]
    text = _MD_IMAGE_RE.sub('', text)  # inline images are embedded separately
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


# Zendesk "Customer IP Address" — the website submission IP (docs/ZENDESK_FIELDS.md).
SUBMISSION_IP_FIELD_ID = 14438419565340
_IPV4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')


def _custom_field_value(ticket: dict, field_id: int):
    for cf in (ticket.get('custom_fields') or []):
        if cf.get('id') == field_id:
            return cf.get('value')
    return None


def _is_private_ip(ip: str) -> bool:
    parts = ip.split('.')
    if len(parts) != 4:
        return True
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return True
    return (a in (10, 127, 0) or a >= 224 or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168) or (a == 169 and b == 254))


def _email_candidate_ips(raw_headers: str) -> set:
    """Public IPv4s appearing anywhere in an email's stored headers (the
    sender's originating IP is normally among them)."""
    return {ip for ip in _IPV4_RE.findall(raw_headers or '') if not _is_private_ip(ip)}


def _identity_context(dispute, ticket: dict, claim_emails: Optional[list] = None) -> dict:
    """Cross-check the website submission IP against the IP(s) the client later
    emailed from. `matched` is True ONLY on an exact IP match (else the report
    stays silent, per the brief). Also counts the client's own messages.

    Pass `claim_emails` (prefetched EmailLog rows for the claim) to reuse the same
    fetch build_dispute_evidence_bundle does for the communication history."""
    out = {'submission_ip': '', 'matched': False, 'matched_at': None, 'client_msg_count': 0}
    claim = dispute.claim
    if not claim:
        return out
    out['submission_ip'] = str(_custom_field_value(ticket, SUBMISSION_IP_FIELD_ID) or '').strip()
    client_email = (claim.client_email or '').lower()
    if not client_email:
        return out
    sub_ip = out['submission_ip']
    if claim_emails is None:
        rows = EmailLog.objects.filter(claim=claim).order_by('received_at')
    else:
        rows = sorted(claim_emails, key=lambda e: e.received_at)
    for el in rows:
        if (el.from_email or '').lower() != client_email:
            continue
        out['client_msg_count'] += 1
        if sub_ip and not out['matched'] and sub_ip in _email_candidate_ips(el.raw_headers or ''):
            out['matched'] = True
            out['matched_at'] = el.received_at
    return out


def _parse_dt(value):
    """Parse a tz-aware datetime from an ISO string, or pass a datetime through."""
    if not value:
        return None
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except Exception:
        return None


def _build_timeline(dispute, comments: list) -> list:
    """Chronological case milestones — [{'when': 'Feb 03, 2026', 'label': ...}]."""
    claim = dispute.claim
    events = []
    if claim and getattr(claim, 'created_at', None):
        events.append((claim.created_at, 'Claim submitted by the customer on our website'))
    if dispute.transaction_date:
        events.append((dispute.transaction_date, 'Payment made and service authorised'))
    public_times = [_parse_dt(c.get('created_at')) for c in comments if c.get('public')]
    public_times = [t for t in public_times if t]
    if public_times:
        events.append((min(public_times), 'First contacted the customer'))
        if max(public_times) != min(public_times):
            events.append((max(public_times), 'Most recent update sent to the customer'))
    if dispute.pk and dispute.created_at:
        events.append((dispute.created_at, 'PayPal dispute received'))
    events = [(t, label) for (t, label) in events if t is not None]
    events.sort(key=lambda e: e[0])
    return [{'when': t.strftime('%b %d, %Y'), 'label': label} for t, label in events]


def _consent_clause(consent: dict) -> str:
    """' when they submitted the claim on <date>, from IP <ip>' — built from the
    Zendesk ticket-creation time (the form is filed the instant it's submitted,
    so that IS the consent moment) and the submission IP. Blank when unknown."""
    consent = consent or {}
    when, ip = consent.get('when'), consent.get('ip')
    if when and ip:
        return f" when they submitted the claim on {when}, from IP {ip}"
    if when:
        return f" when they submitted the claim on {when}"
    if ip:
        return f" from IP {ip}"
    return ""


def _bottom_line(dispute, identity: dict, consent: Optional[dict] = None) -> list:
    """Reason-specific 'bottom line up front' bullets — the single strongest
    argument for THIS dispute reason, stated plainly for a skimming reviewer."""
    claim = dispute.claim
    name = (claim.client_name if claim else '') or dispute.buyer_name or 'The customer'
    reason = dispute.dispute_reason
    clause = _consent_clause(consent)
    bullets = []
    if reason == 'UNAUTHORISED':
        bullets.append(f"{name} submitted this claim themselves on our website, providing their own "
                       "flight, contact and lost-item details.")
        bullets.append(f"They accepted our Terms & Conditions and the 24-hour refund window{clause}.")
        if identity.get('matched'):
            bullets.append("They later contacted us from the very same IP address used to submit the "
                           f"claim ({identity['submission_ip']}) — confirming this was the same person.")
        elif identity.get('client_msg_count'):
            bullets.append("They corresponded with us afterwards from their own email — behaviour "
                           "inconsistent with an unauthorised transaction.")
    elif reason in ('MERCHANDISE_OR_SERVICE_NOT_RECEIVED', 'MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED'):
        bullets.append("The service was performed: we reported the lost item to the airline and the "
                       "airport lost-&-found offices and kept the customer updated throughout.")
        bullets.append(f"{name} accepted our Terms and the 24-hour refund window{clause}; the "
                       "service fee is non-refundable after that window.")
    elif reason == 'CREDIT_NOT_PROCESSED':
        bullets.append(f"{name} accepted our refund policy, including the 24-hour window{clause}.")
        bullets.append("We performed the search service they paid for; no further credit is due under "
                       "that policy.")
    else:
        bullets.append(f"{name} authorised this purchase and we performed the search service they paid for.")
        bullets.append(f"They accepted our Terms and the 24-hour refund window{clause}.")
    return bullets


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
SECTION_TITLES = dict(SECTION_ORDER)
_DEFAULT_SECTION_PRIORITY = [k for k, _ in SECTION_ORDER]

# Per-reason section ordering: lead with the evidence class that wins THAT
# dispute type. Unauthorised → who-authorised-it first (intake + interactions);
# not-received/not-as-described → proof-of-work first (submissions + updates).
SECTION_PRIORITY_BY_REASON = {
    'UNAUTHORISED': ['SERVICE_INITIATION', 'INTERACTIONS', 'FLIGHT_IDENTIFICATION',
                     'SUBMISSIONS', 'CLAIM_UPDATES', 'OTHER'],
    'MERCHANDISE_OR_SERVICE_NOT_RECEIVED': ['SUBMISSIONS', 'CLAIM_UPDATES', 'INTERACTIONS',
                                            'FLIGHT_IDENTIFICATION', 'SERVICE_INITIATION', 'OTHER'],
    'MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED': ['SERVICE_INITIATION', 'SUBMISSIONS',
                                                'INTERACTIONS', 'CLAIM_UPDATES',
                                                'FLIGHT_IDENTIFICATION', 'OTHER'],
}


def _section_priority_for(reason: str) -> list:
    return SECTION_PRIORITY_BY_REASON.get(reason, _DEFAULT_SECTION_PRIORITY)

EVIDENCE_NARRATIVE_SYSTEM_PROMPT = (
    "You are an employee of Airport Lost & Found (ALF), a paid lost-item "
    "recovery service, preparing ALF's own evidence for a PayPal dispute. You "
    "are given numbered evidence records from our support system for one case. "
    "For EACH record, decide:\n"
    "1. section — the best fit from: SERVICE_INITIATION (the customer's own "
    "claim submission / intake), FLIGHT_IDENTIFICATION (us verifying the "
    "flight), INTERACTIONS (us contacting the customer by phone/email), "
    "SUBMISSIONS (us reporting the lost item to airline/airport lost-&-found "
    "offices), CLAIM_UPDATES (status updates, item-found / return options), "
    "OTHER (relevant but uncategorised), or EXCLUDE.\n"
    "Use EXCLUDE for internal automation logs, system noise (e.g. abandoned-cart "
    "notices), duplicates, or anything that does NOT help our defence.\n"
    "2. explanation — ONE confident sentence, written in the FIRST PERSON as "
    "ALF: use 'we', 'our' and 'us', and call the buyer 'the customer'. NEVER "
    "refer to ALF as 'the merchant' or in the third person. Say what the record "
    "shows and why it supports us (that the customer authorised us and that we "
    "performed the service they paid for). Vary your wording — do not start "
    "every sentence with 'This record'. Base it ONLY on the record text; never "
    "invent facts.\n"
    "Be careful with negative-outcome records: a note that the item or flight "
    "was NOT found does not help our defence — prefer EXCLUDE, or include it "
    "only where it clearly shows the effort we made, and never imply we failed "
    "to deliver our service.\n"
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


def _group_into_sections(items: list, narrative: Optional[dict], reason: str = '') -> list:
    """Group evidence items into narrative sections, ordered for THIS dispute
    reason. With a narrative mapping, place/caption/exclude per the AI; without
    one, return a single ungrouped 'Case record' section (graceful fallback)."""
    if not items:
        return []
    if not narrative:
        return [{'key': 'ALL', 'title': 'Case record',
                 'items': [_item_entry(it) for it in items]}]

    buckets = {key: [] for key in SECTION_TITLES}
    for it in items:
        placement = narrative.get(it['index']) or {}
        section = placement.get('section') or 'OTHER'
        if section == 'EXCLUDE':
            continue
        if section not in buckets:
            section = 'OTHER'
        buckets[section].append(_item_entry(it, placement.get('explanation', '')))
    return [{'key': key, 'title': SECTION_TITLES[key], 'items': buckets[key]}
            for key in _section_priority_for(reason) if buckets.get(key)]


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
    section. Also includes claim evidence images, the email history, the fixed
    report assets, and category framing.
    """
    zd_data = _fetch_zendesk_ticket_full(dispute.zd_ticket_id)
    ticket = zd_data.get('ticket', {})
    comments = zd_data.get('comments', [])

    # Fetch the claim's emails ONCE and share them with both the communication
    # history and the identity cross-check (was two queries for the same rows).
    claim_emails = (list(EmailLog.objects.filter(claim=dispute.claim))
                    if dispute.claim else [])

    evidence_list = _fetch_claim_evidence_base64(dispute.claim) if dispute.claim else []
    communication_history = _fetch_communication_history(dispute, claim_emails=claim_emails)
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
            'has_image': bool(p['images']), 'text': (p['body'] or '')[:_EVIDENCE_RECORD_TEXT_CHARS], 'panel': p,
        })

    narrative = _narrate_evidence(dispute, items, dispute.claim) if use_ai else None
    sections = _group_into_sections(items, narrative, reason=dispute.dispute_reason)

    identity = _identity_context(dispute, ticket, claim_emails=claim_emails)
    claim = dispute.claim
    # The Zendesk ticket is created the instant the form is submitted, so its
    # creation time is the consent moment; pair it with the submission IP.
    consent = {'when': _fmt_zd_time(ticket.get('created_at')), 'ip': identity.get('submission_ip', '')}

    return {
        'dispute': dispute,
        'claim': claim,
        'ticket': ticket,
        'comments': comments,
        'panels': panels,
        'flight_card': flight_card,
        'sections': sections,
        'narrative': _narrative_fields(dispute),
        'framing': framing,
        'bottom_line': _bottom_line(dispute, identity, consent),
        'timeline': _build_timeline(dispute, comments),
        'identity': identity,
        'consent': consent,
        'alias_used': bool(getattr(claim, 'email_alias', '')) if claim else False,
        'assets': {
            'homepage': _asset_data_uri('homepage.jpg'),
            'checkout': _asset_data_uri('checkout_annotated.jpg'),
        },
        'claim_evidence': evidence_list,
        'communication_history': communication_history,
        'category': dispute.dispute_reason,
        'category_label': dispute.get_dispute_reason_display() if dispute.dispute_reason else '',
        'generated_at': dj_timezone.localtime().strftime('%Y-%m-%d %H:%M:%S'),
    }


# PayPal caps the dispute evidence/supporting-info `notes` field near 2000
# characters. We do NOT hard-truncate (that could cut a restored name mid-word
# and the manager reviews/edits before submitting) — we warn past this length.
PAYPAL_NOTES_MAX_CHARS = 2000

EVIDENCE_NOTES_SYSTEM_PROMPT = (
    "You are an employee of Airport Lost & Found (ALF), a paid lost-item "
    "recovery service, writing ALF's own evidence narrative for a PayPal "
    "dispute. PayPal's dispute reviewer reads this text to decide the case in "
    "our favour or the customer's, so write a confident, factual, first-person "
    "case.\n"
    "VOICE: always 'we', 'our', 'us' for ALF; call the buyer 'the customer'. "
    "NEVER call ALF 'the merchant' or write in the third person.\n"
    "Produce FOUR sections as JSON string fields:\n"
    "- opening: one short paragraph stating we are formally contesting this "
    "dispute and, in one line, why it is unfounded for THIS dispute reason.\n"
    "- authorization: the proof the customer themselves authorised this "
    "purchase — that they personally submitted the claim on our website "
    "(citing the date/IP if given), supplying details only they could provide "
    "(their flight, where they lost the item, a description of the item), and "
    "that they accepted our Terms & Conditions and refund window. Cite the "
    "specific values you are given.\n"
    "- service_delivery: the proof we performed the paid service — the fee the "
    "customer paid and our reference, plus the work shown in the case records "
    "(verifying the flight, contacting the customer, reporting the lost item to "
    "the airline and airport lost-&-found offices, and sending status "
    "updates).\n"
    "- closing: a brief, explicit request that PayPal resolve the dispute in "
    "our favour.\n"
    "RULES: Use ONLY the facts and case records provided in the user message. "
    "NEVER invent a fact, date, name, amount, flight, or action; if a value is "
    "missing or marked unknown, leave it out rather than guess. If a "
    "'manager_emphasis' note is provided, weave its point in where it fits. "
    "Keep each section tight and free of padding. Return JSON: {\"opening\": "
    "<str>, \"authorization\": <str>, \"service_delivery\": <str>, "
    "\"closing\": <str>}."
)


def _lead_with_service(reason: str) -> bool:
    """Whether the service-delivery proof should lead over the authorisation
    proof for this dispute reason (not-received / not-as-described win on
    proof-of-work; everything else leads with who-authorised-it). Derived from
    the same per-reason ordering the report uses (SECTION_PRIORITY_BY_REASON)."""
    return _section_priority_for(reason)[0] in ('SUBMISSIONS', 'CLAIM_UPDATES')


def _assemble_narrative_notes(sections: dict, reason: str = '') -> str:
    """Join the four narrative sections into the single plain-text `notes` body
    PayPal receives. The two middle proofs are ordered (and numbered) for THIS
    dispute reason; empty sections are skipped."""
    opening = (sections.get('opening') or '').strip()
    closing = (sections.get('closing') or '').strip()
    middle = [
        ('Proof the customer authorised this purchase', (sections.get('authorization') or '').strip()),
        ('Proof we delivered the paid service', (sections.get('service_delivery') or '').strip()),
    ]
    if _lead_with_service(reason):
        middle.reverse()

    parts = []
    if opening:
        parts.append(opening)
    n = 1
    for label, text in middle:
        if text:
            parts.append(f"{n}. {label}\n{text}")
            n += 1
    if closing:
        parts.append(closing)
    return "\n\n".join(parts).strip()


def _narrative_untrusted(bundle: dict, max_comments: int = 8, per_comment_chars: int = 400) -> dict:
    """The case records the AI may ground the service-delivery section in: the
    cleaned Zendesk comment bodies, fenced under the approved 'zendesk_comment'
    tag. Empty when there are none."""
    bodies = []
    for p in bundle.get('panels', [])[:max_comments]:
        body = (p.get('body') or '').strip()
        if not body:
            continue
        tag = 'public reply to the customer' if p.get('public') else 'internal note'
        bodies.append(f"({tag}): {body[:per_comment_chars]}")
    return {'zendesk_comment': bodies} if bodies else {}


def _dispute_narrative_facts(dispute, bundle: dict, *, manager_note: str = '') -> dict:
    """Trusted, structured case facts handed to the narrative LLM (tokenized by
    AIClient before sending, restored on the way out). Missing values are marked
    so the model omits them rather than guessing."""
    nf = bundle['narrative']
    identity = bundle['identity']
    consent = bundle['consent']
    flight = bundle['flight_card']
    claim = bundle['claim']
    framing = bundle['framing']

    facts = {
        'dispute_reason': dispute.get_dispute_reason_display() if dispute.dispute_reason else 'uncategorised',
        'why_unfounded': framing['lead'],
        'customer_name': nf['client_name'],
        'our_reference': nf['alf_id'] or '(none)',
        'service_fee': f"{nf['currency']} {nf['fee']}" if nf['fee'] is not None else '(unknown)',
        'item_lost': nf['object'] or '(not recorded)',
        'lost_location': ((getattr(claim, 'lost_location', '') or '').strip()[:_LOST_LOCATION_DISPLAY_CHARS]) if claim else '',
        'flight': (f"{flight['airline']} {flight['number']} {flight['from_iata']}→{flight['to_iata']}"
                   if flight else '(not recorded)'),
        'claim_submitted_on': consent.get('when') or '(unknown)',
        'submission_ip': consent.get('ip') or '(unknown)',
        'terms_url': nf['terms_url'],
        'contacted_us_from_same_ip': 'yes' if identity.get('matched') else 'no',
        'customer_messages_to_us': str(identity.get('client_msg_count') or 0),
        'updates_we_sent': str(sum(1 for p in bundle.get('panels', []) if p.get('public'))),
        'strongest_points': ' '.join(bundle.get('bottom_line', [])),
    }
    if (manager_note or '').strip():
        facts['manager_emphasis'] = manager_note.strip()
    return facts


def _fallback_narrative_sections(dispute, bundle: dict) -> dict:
    """Deterministic narrative used when the LLM is unconfigured or errors —
    same four-section structure, filled only from case fields (never fabricated)."""
    nf = bundle['narrative']
    identity = bundle['identity']
    consent = bundle['consent']
    flight = bundle['flight_card']
    name = nf['client_name']
    alf = nf['alf_id']
    fee = f"{nf['currency']} {nf['fee']}" if nf['fee'] is not None else 'the agreed service fee'
    item = nf['object']
    clause = _consent_clause(consent)  # " when they submitted the claim on <when>, from IP <ip>"

    opening = (
        f"We are formally contesting this PayPal dispute. {name} purchased our paid "
        "lost-item recovery service, authorised us to act on their behalf, and we carried "
        "out that service in full. The points below set out the evidence."
    )

    auth = [f"{name} personally submitted this claim through our website{clause}."]
    if item:
        auth.append("The submission included details only the customer could provide — "
                    f"their flight, where the item was lost, and a description of the item ({item}).")
    else:
        auth.append("The submission included details only the customer could provide, including "
                    "their flight, where the item was lost, and a description of the item.")
    auth.append("In submitting the claim, the customer accepted our Terms and Conditions and "
                f"refund window ({nf['terms_url']}).")
    if identity.get('matched'):
        auth.append("The customer later contacted us from the very same IP address used to submit "
                    f"the claim ({identity['submission_ip']}), confirming this was the same person.")
    elif identity.get('client_msg_count'):
        auth.append("The customer corresponded with us from their own email afterwards — behaviour "
                    "inconsistent with an unauthorised transaction.")
    authorization = ' '.join(auth)

    svc = [f"The customer paid {fee} for our service" + (f" (our reference {alf})." if alf else ".")]
    if flight:
        svc.append(f"We verified the flight involved ({flight['airline']} {flight['number']}, "
                   f"{flight['from_iata']}→{flight['to_iata']}).")
    svc.append("We reported the lost item to the relevant airline and airport lost-and-found "
               "offices and kept the customer updated on the search.")
    updates = sum(1 for p in bundle.get('panels', []) if p.get('public'))
    if updates:
        svc.append(f"We sent the customer {updates} update{'s' if updates != 1 else ''} during the case.")
    service_delivery = ' '.join(svc)

    closing = ("Because the customer authorised this purchase and we delivered the service they "
               "paid for, we respectfully request that PayPal resolve this dispute in our favour.")

    return {'opening': opening, 'authorization': authorization,
            'service_delivery': service_delivery, 'closing': closing}


def build_dispute_narrative_notes(dispute, *, manager_note: str = '', use_ai: bool = True) -> dict:
    """Write ALF's first-person evidence narrative (the `notes` text PayPal's
    reviewer reads) for a dispute.

    Returns ``{'notes': <assembled text>, 'source': 'AI'|'FALLBACK',
    'sections': {opening, authorization, service_delivery, closing}}``.

    The AI path masks the customer's name/email/address/phone before the LLM
    sees anything and restores the real values on the way out (PayPal is inside
    the trust zone and must receive the real values). Any AI failure — no key,
    network, or a malformed reply — falls back to a deterministic template
    narrative with the same structure. The manager reviews/edits before submit.
    """
    bundle = build_dispute_evidence_bundle(dispute, embed_attachments=False, use_ai=False)
    claim = bundle['claim']
    sections = None
    source = 'FALLBACK'

    if use_ai:
        try:
            ss = SystemSettings.get_instance()
            if getattr(ss, 'ai_api_key', ''):
                from apps.ai.client import AIClient
                from apps.ai.schemas import DisputeNarrative
                result = AIClient.complete(
                    system_prompt=EVIDENCE_NOTES_SYSTEM_PROMPT,
                    trusted=_dispute_narrative_facts(dispute, bundle, manager_note=manager_note),
                    untrusted=_narrative_untrusted(bundle),
                    known_pii=_known_pii_for(claim),
                    response_schema=DisputeNarrative,
                    call_site='dispute_narrative_notes',
                    temperature=0.4,
                    max_tokens=1200,
                )
                sections = {
                    'opening': result.opening,
                    'authorization': result.authorization,
                    'service_delivery': result.service_delivery,
                    'closing': result.closing,
                }
                source = 'AI'
        except Exception as e:
            logger.warning(f"Dispute narrative AI unavailable; using deterministic fallback: {e}")
            sections = None

    if sections is None:
        sections = _fallback_narrative_sections(dispute, bundle)
        source = 'FALLBACK'

    notes = _assemble_narrative_notes(sections, reason=dispute.dispute_reason)
    if len(notes) > PAYPAL_NOTES_MAX_CHARS:
        logger.warning(
            "Dispute #%s narrative is %d chars — PayPal caps dispute notes near %d; "
            "the manager should trim before submitting.",
            getattr(dispute, 'pk', '?'), len(notes), PAYPAL_NOTES_MAX_CHARS,
        )
    return {'notes': notes, 'source': source, 'sections': sections}


_TIMELINE_MIN_DT = datetime.min.replace(tzinfo=_std_timezone.utc)


def build_dispute_reply_timeline(dispute) -> list:
    """Chronological back-and-forth for the dispute page (feature D).

    Merges our own submissions (DisputeSubmission rows) with PayPal's recorded
    evidences[] and buyer/seller messages[] from the stored payload into one
    ordered list (oldest first; entries without a timestamp sort to the end).
    Each entry: {when, when_str, actor, kind, title, status, source, text, ...}.
    Read-only — purely for display.
    """
    entries = []

    _SUB_TITLES = {'EVIDENCE': 'Evidence submitted',
                   'SUPPORTING_INFO': 'Supporting info submitted',
                   'MESSAGE': 'Message sent'}
    for s in dispute.submissions.all():
        when = s.submitted_at or s.created_at
        if s.status == 'DRAFT':
            title = 'Draft prepared (not sent)'
        elif s.status == 'FAILED':
            title = 'Submission failed'
        else:
            title = _SUB_TITLES.get(s.kind, 'Submission')
        entries.append({
            'when': when, 'when_str': _fmt_zd_time(when), 'actor': 'Airport Lost & Found',
            'kind': 'submission', 'title': title, 'status': s.status,
            'source': s.get_source_display(), 'text': (s.notes or '')[:_CASE_LOG_TEXT_DISPLAY_CHARS],
            'image_count': s.images.count(), 'attached_pdf': s.attach_evidence_pdf,
        })

    payload = dispute.raw_webhook_payload or {}

    # PayPal records the buyer's opening complaint BOTH as a SUBMITTED_BY_BUYER
    # CREATE evidence AND as a buyer message[] — identical text and time. Collect
    # the buyer messages first so that duplicate evidence can be dropped (and the
    # buyer's words are NEVER mislabelled as ours, which is what made the buyer's
    # "this website is a scam" complaint show under "Airport Lost & Found").
    def _norm(t):
        return ' '.join((t or '').split())
    _buyer_msg_texts = {_norm(m.get('content'))
                        for m in (payload.get('messages') or [])
                        if (m.get('posted_by') or '').upper() == 'BUYER'}

    for ev in (payload.get('evidences') or []):
        src = (ev.get('source') or '').upper()
        etype = (ev.get('evidence_type') or '').upper()
        notes = ev.get('notes') or ''
        if src in ('SUBMITTED_BY_BUYER', 'REQUESTED_FROM_BUYER'):
            # The buyer's own words. Skip it if the message thread already carries
            # the same text (the opening complaint), else show it as the Buyer.
            if _norm(notes) and _norm(notes) in _buyer_msg_texts:
                continue
            actor = 'Buyer'
            title = 'Buyer opened the dispute' if etype == 'CREATE' else 'Buyer submitted to PayPal'
        elif src == 'REQUESTED_FROM_SELLER':
            actor, title = 'PayPal', 'PayPal requested information'
        elif src == 'SUBMITTED_BY_SELLER':
            # We submitted this — say so plainly (the old 'On file at PayPal'
            # left the manager unsure whether it had actually been sent).
            actor, title = 'Airport Lost & Found', 'Submitted to PayPal'
        else:
            # Unknown/other source: recorded at PayPal but not clearly ours —
            # never claim it under our name.
            actor, title = 'PayPal', 'On file at PayPal'
        when = _parse_dt(ev.get('date') or ev.get('create_time'))
        docs = ev.get('documents')
        if not isinstance(docs, list):
            docs = (ev.get('evidence_info') or {}).get('documents') or []
        # PayPal's bookkeeping types CREATE (dispute-open) and OTHER (a bare
        # request) carry no meaning to a human — showing them rendered an empty,
        # cryptic "OTHER" card. Only surface an informative evidence type.
        shown_type = '' if etype in ('', 'OTHER', 'CREATE') else ev.get('evidence_type', '')
        entries.append({
            'when': when, 'when_str': _fmt_zd_time(when), 'actor': actor,
            'kind': 'paypal_evidence', 'title': title, 'status': '',
            'source': shown_type, 'text': notes[:_CASE_LOG_TEXT_DISPLAY_CHARS],
            'doc_count': len(docs) if isinstance(docs, list) else 0,
        })

    for m in (payload.get('messages') or []):
        by = (m.get('posted_by') or '').upper()
        actor = ('Buyer' if by == 'BUYER'
                 else 'PayPal' if by in ('ARBITER', 'PAYPAL')
                 else 'Airport Lost & Found')
        when = _parse_dt(m.get('time_posted') or m.get('create_time'))
        entries.append({
            'when': when, 'when_str': _fmt_zd_time(when), 'actor': actor,
            'kind': 'paypal_message', 'title': 'Message', 'status': '',
            'source': '', 'text': (m.get('content') or '')[:_CASE_LOG_TEXT_DISPLAY_CHARS],
        })

    entries.sort(key=lambda e: (e['when'] is None, e['when'] or _TIMELINE_MIN_DT))
    return entries


def generate_evidence_report(dispute_id: int) -> Optional[DisputeDocument]:
    """
    Generate a comprehensive evidence report for a dispute.
    
    This is a template-based (NO AI) structured factual report that compiles:
    - Ticket data (rendered as simulated Zendesk panels)
    - Claim evidence
    - Communication history

    Steps:
    1. Fetch Dispute + Zendesk ticket data
    2. Fetch claim evidence images
    3. Fetch communication history (emails)
    4. Render template-based report (structured, factual)
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
        evidence_list = template_context['claim_evidence']
        communication_history = template_context['communication_history']
        logger.info(
            f"Bundle for Dispute #{dispute_id}: "
            f"{len(evidence_list)} evidence, {len(communication_history)} emails")

        html_string = render_to_string(report_template_for(dispute), template_context)

        # Generate PDF
        pdf_bytes = _render_to_pdf(html_string, f"Dispute #{dispute_id} Evidence Report")

        if not pdf_bytes:
            logger.error(f"Failed to generate PDF for Dispute #{dispute_id}")
            return None

        if len(pdf_bytes) > PAYPAL_EVIDENCE_SIZE_WARN_BYTES:
            logger.warning(
                f"Evidence report for Dispute #{dispute_id} is {len(pdf_bytes) // (1024 * 1024)}MB — "
                f"PayPal evidence uploads are typically capped near 10MB; consider trimming embedded images.")

        # Persist via the shared helper — auto-versioned filename/content/version/
        # log in one narrow transaction. The slow work (Zendesk fetch, render) is
        # already done above, outside any transaction.
        document = _persist_document(
            dispute,
            doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT,
            generated_by=DisputeDocument.GENERATED_BY_MANUAL,
            content_html=html_string,
            pdf_bytes=pdf_bytes,
            details=(f"Evidence report created. Evidence: {len(evidence_list)}, "
                     f"Emails: {len(communication_history)}, PDF size: {len(pdf_bytes)} bytes"),
        )

        logger.info(f"Successfully generated evidence report for Dispute #{dispute_id} (Document #{document.id})")
        return document
        
    except Exception:
        logger.exception(f"Error generating evidence report for Dispute #{dispute_id}")
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

        # Only the evidence report is generated now (the response letter was
        # dropped — the written argument is plain text on a DisputeSubmission).
        # Legacy RESPONSE_LETTER rows can't be regenerated; surface that instead
        # of silently producing an evidence report of the wrong type.
        if old_document.doc_type != DisputeDocument.DOC_TYPE_EVIDENCE_REPORT:
            logger.warning(
                f"Refusing to regenerate document #{document_id}: only evidence "
                f"reports are generated now (doc_type={old_document.doc_type}).")
            return None

        # generate_evidence_report auto-increments the version (max + 1) and writes
        # its own single DOCUMENT_GENERATED log, so the new doc is already correctly
        # versioned and filename/content/log all agree.
        return generate_evidence_report(dispute.id)

    except DisputeDocument.DoesNotExist:
        logger.error(f"Document #{document_id} not found")
        return None
    except Exception:
        logger.exception(f"Error regenerating document #{document_id}")
        return None
