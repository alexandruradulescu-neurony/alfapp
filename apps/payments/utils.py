"""
Payment utilities for LORA.
Includes PDF generation for proof of work documents.
"""

import base64
import logging
import os
from datetime import datetime
from typing import List, Optional

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.template.loader import render_to_string

from apps.claims.models import Claim

logger = logging.getLogger(__name__)

# Extension → MIME for the embedded evidence data-URIs.
_EVIDENCE_MIME_BY_EXT = {
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'png': 'image/png',
    'gif': 'image/gif',
    'webp': 'image/webp',
}

# Print stylesheet for the proof-of-work PDF. Kept as a module constant (rather
# than inline in the generator) so the function body stays about orchestration.
_PROOF_OF_WORK_CSS = '''
    @page {
        size: A4;
        margin: 2cm;
        @bottom-right {
            content: "Page " counter(page) " of " counter(pages);
            font-size: 10pt;
        }
    }
    body {
        font-family: "Segoe UI", Arial, sans-serif;
        line-height: 1.6;
        color: #333;
    }
    .header {
        border-bottom: 2px solid #0070d2;
        padding-bottom: 10px;
        margin-bottom: 20px;
    }
    .header h1 {
        color: #0070d2;
        margin: 0;
    }
    .section {
        margin-bottom: 25px;
    }
    .section h2 {
        color: #0070d2;
        border-bottom: 1px solid #ddd;
        padding-bottom: 5px;
    }
    .claim-summary {
        background: #f5f5f5;
        padding: 15px;
        border-radius: 5px;
    }
    .comment {
        border-left: 3px solid #0070d2;
        padding-left: 15px;
        margin-bottom: 15px;
    }
    .comment-author {
        font-weight: bold;
        color: #555;
    }
    .comment-date {
        font-size: 0.85em;
        color: #888;
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
    .evidence-description {
        font-style: italic;
        color: #555;
        margin-top: 5px;
    }
    .no-data {
        color: #888;
        font-style: italic;
    }
'''


def _get_weasyprint():
    """
    Lazily import WeasyPrint to avoid import errors when library is not installed.
    """
    try:
        from weasyprint import HTML, CSS
        return HTML, CSS
    except Exception as e:
        logger.error(f"WeasyPrint not available: {e}")
        logger.info("Install GTK+ and WeasyPrint: https://doc.courtbouillon.org/weasyprint/stable/first_steps.html")
        return None, None


def _evidence_within_media_root(evidence) -> bool:
    """Path-traversal guard for filesystem storage: True if the evidence file
    lives under MEDIA_ROOT. Uses commonpath (not startswith) so a sibling dir
    like '/srv/media_evil' can't pass when MEDIA_ROOT is '/srv/media'. Remote
    backends (no local .path) skip the check and rely on the storage API."""
    try:
        abs_path = os.path.abspath(evidence.image.path)
    except NotImplementedError:
        return True  # non-filesystem storage (e.g. S3) — nothing to validate
    except (SuspiciousFileOperation, ValueError) as e:
        # Django's storage refused to even resolve a traversing path — that IS
        # the guard firing; treat it as outside MEDIA_ROOT.
        logger.warning(f"Evidence file path rejected as outside MEDIA_ROOT: {e}")
        return False
    media_root = os.path.abspath(settings.MEDIA_ROOT)
    try:
        within = os.path.commonpath([abs_path, media_root]) == media_root
    except ValueError:
        within = False  # different roots / drives → definitely not inside
    if not within:
        logger.warning(f"Evidence file path outside MEDIA_ROOT: {abs_path}")
        return False
    return True


def _gather_evidence_images(claim: Claim) -> List[dict]:
    """Read each evidence image once, stream it via the storage API (file handle
    always closed), and return [{description, uploaded_at, data_uri}, …]. Skips
    any image that fails the MEDIA_ROOT guard or errors out individually."""
    images: List[dict] = []
    # .iterator() so a claim with many images doesn't cache the whole queryset.
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
            file_ext = evidence.image.name.split('.')[-1].lower()
            mime_type = _EVIDENCE_MIME_BY_EXT.get(file_ext, 'image/jpeg')
            images.append({
                'description': evidence.description,
                'uploaded_at': evidence.uploaded_at,
                'data_uri': f"data:{mime_type};base64,{image_base64}",
            })
        except Exception as e:
            logger.warning(f"Error processing evidence {evidence.id}: {e}")
            continue
    return images


def generate_proof_of_work_pdf(claim: Claim) -> Optional[bytes]:
    """
    Generate a PDF proof of work document for a claim.

    Steps:
    a) Gather claim.evidence images as base64 data URIs (_gather_evidence_images)
    b) Call fetch_zendesk_comments(claim.zd_ticket_id) from integrations service
    c) Render template with claim data, comments, and images
    d) Convert rendered HTML to PDF using WeasyPrint
    e) Return PDF as bytes

    Args:
        claim: The Claim instance to generate PDF for

    Returns:
        PDF as bytes, or None on failure
    """
    try:
        # a) Gather all evidence images (read once, handles closed, path-guarded)
        evidence_list = _gather_evidence_images(claim)

        # b) Fetch Zendesk comments
        zendesk_comments = []
        if claim.zd_ticket_id:
            try:
                from apps.integrations.services import fetch_zendesk_comments
                zendesk_comments = fetch_zendesk_comments(claim.zd_ticket_id)
            except Exception as e:
                logger.warning(f"Error fetching Zendesk comments for claim {claim.id}: {e}")
                zendesk_comments = []
        
        # d) Prepare context for template
        context = {
            'claim': claim,
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'evidence_list': evidence_list,
            'zendesk_comments': zendesk_comments,
            'evidence_count': len(evidence_list),
            'comment_count': len(zendesk_comments),
        }
        
        # Render HTML template
        html_string = render_to_string('proof_of_work.html', context)
        
        # e) Convert to PDF using WeasyPrint
        HTML, CSS = _get_weasyprint()
        
        if not HTML:
            logger.error("WeasyPrint is not installed. Cannot generate PDF.")
            return None
        
        try:
            html = HTML(string=html_string)
            css = CSS(string=_PROOF_OF_WORK_CSS)
            pdf_bytes = html.write_pdf(stylesheets=[css])
            
            logger.info(f"Generated PDF proof of work for claim #{claim.id} ({len(pdf_bytes)} bytes)")
            return pdf_bytes
            
        except Exception as e:
            logger.error(f"WeasyPrint error generating PDF for claim {claim.id}: {e}")
            return None
        
    except Exception as e:
        logger.error(f"Error generating proof of work PDF for claim {claim.id}: {e}")
        return None


def generate_dispute_notification_email(claim: Claim) -> str:
    """
    Generate an email body for dispute notification.
    
    Args:
        claim: The Claim instance
        
    Returns:
        Email body as string
    """
    return f"""
Dear LORA Team,

A PayPal dispute has been opened for the following claim:

Claim ID: {claim.id}
Customer Email: {claim.client_email}
Status: {claim.status}
Flight Details: {claim.flight_details or 'Not provided'}

Please review this case promptly and take appropriate action.

This is an automated notification from the LORA system.
"""
