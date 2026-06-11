"""
IMAP email processing service for LORA.
Processes incoming emails and uses Qwen AI for analysis.
Phase 8: Smart Email Processing Overhaul with alias-based matching and AI categorization.
"""

import imaplib
import email
import json
import re
import logging
from datetime import timedelta
from email.header import decode_header
from typing import Optional, Dict, Any, List, Tuple

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.communications.models import EmailLog
from apps.integrations.services import (
    add_zendesk_ticket_tags,
    match_alias_to_zendesk_ticket,
    post_zendesk_comment,
)
from apps.ai.exceptions import AIResponseValidationError

logger = logging.getLogger(__name__)

# Maximum number of emails to process per run
MAX_EMAILS_PER_RUN = 20

# How far back any mailbox read ever looks. The inbox holds years of mail;
# LORA's window is always the last two days, read or unread state untouched
# beyond it.
EMAIL_LOOKBACK_DAYS = 2

# IMAP SINCE wants RFC 3501 dates (e.g. 10-Jun-2026) with fixed English
# month names — strftime('%b') is locale-dependent, so spell them out.
IMAP_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

# Categories that can be auto-resolved
AUTO_RESOLVABLE_CATEGORIES = [
    'SUBMISSION_CONFIRMATION',
    'OBJECT_NOT_FOUND',
]

# Category → AI-added Zendesk tag. Routine categories (submission
# confirmations, general correspondence, unknown) are deliberately untagged.
AI_TAG_BY_CATEGORY = {
    'OBJECT_FOUND': 'ai_object_found',
    'OBJECT_NOT_FOUND': 'ai_object_not_found',
    'RESUBMISSION_REQUIRED': 'ai_resubmission_required',
}
# Added whenever the AI says the email needs a human, regardless of category.
AI_TAG_ATTENTION = 'ai_attention_needed'


class EmailNotConfigured(Exception):
    """IMAP credentials are missing from SystemSettings."""


class InvalidAlias(Exception):
    """The ticket's email alias field doesn't look like an email address.

    Guards the IMAP search: the alias is interpolated into the search
    command, so quotes/whitespace in a malformed field value must never
    reach it."""


def imap_since_date(today=None) -> str:
    """The SINCE cutoff: everything from the last EMAIL_LOOKBACK_DAYS days."""
    d = (today or timezone.localdate()) - timedelta(days=EMAIL_LOOKBACK_DAYS)
    return f"{d.day:02d}-{IMAP_MONTHS[d.month - 1]}-{d.year}"


def decode_mime_header(header: str) -> str:
    """
    Decode MIME-encoded email header (e.g., subject, from).
    Handles various encodings like UTF-8, ISO-8859-1, etc.
    """
    if not header:
        return ''

    decoded_parts = []
    for part, encoding in decode_header(header):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
            except (UnicodeDecodeError, LookupError):
                decoded_parts.append(part.decode('utf-8', errors='replace'))
        else:
            decoded_parts.append(part)

    return ''.join(decoded_parts)


def extract_email_body(msg: email.message.Message) -> str:
    """
    Extract the plain text body from an email message.
    Prefers text/plain, falls back to text/html (stripped of tags).
    """
    body_text = ''
    body_html = ''

    # Handle multipart messages
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get('Content-Disposition') or '')

            # Skip attachments
            if 'attachment' in content_disposition:
                continue

            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue

                charset = part.get_content_charset() or 'utf-8'
                part_content = payload.decode(charset, errors='replace')

                if content_type == 'text/plain':
                    body_text = part_content
                elif content_type == 'text/html':
                    body_html = part_content
            except Exception as e:
                logger.warning(f"Error decoding email part: {e}")
                continue
    else:
        # Handle non-multipart messages
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                body_text = payload.decode(charset, errors='replace')
        except Exception as e:
            logger.warning(f"Error decoding email payload: {e}")

    # Prefer plain text, fall back to HTML (strip tags)
    if body_text:
        return body_text.strip()
    elif body_html:
        # Simple HTML tag stripping
        clean_text = re.sub(r'<[^>]+>', '', body_html)
        return clean_text.strip()

    return ''


def extract_from_email(msg: email.message.Message) -> Optional[str]:
    """
    Extract the sender's email address from the From header.
    """
    from_header = msg.get('From', '')
    if not from_header:
        return None

    # Decode if necessary
    from_header = decode_mime_header(from_header)

    # Parse email address from formats like:
    # "John Doe <john@example.com>" or "john@example.com"
    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', from_header)
    if email_match:
        return email_match.group(0).lower()

    # If no angle bracket format, check if the whole thing is an email
    if '@' in from_header:
        return from_header.strip().lower()

    return None


def extract_alias_from_headers(msg: email.message.Message) -> Optional[str]:
    """
    Extract email alias from email headers.
    
    Checks To, Delivered-To, X-Original-To headers for an email address
    that matches the configured email_domain from SystemSettings.
    
    Args:
        msg: The parsed email message
        
    Returns:
        The matched alias email address if found and matches domain, None otherwise
    """
    try:
        # Get configured email domain
        system_settings = SystemSettings.get_instance()
        email_domain = system_settings.email_domain
        
        if not email_domain:
            logger.debug("Email domain not configured in SystemSettings")
            return None
        
        # Headers to check for alias (in order of preference)
        headers_to_check = ['Delivered-To', 'X-Original-To', 'To', 'X-RCPT-TO']
        
        for header_name in headers_to_check:
            header_value = msg.get(header_name, '')
            if not header_value:
                continue
            
            # Decode header if necessary
            header_value = decode_mime_header(header_value)
            
            # Extract email address from header
            email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', header_value)
            if email_match:
                matched_email = email_match.group(0).lower()
                
                # Check if it matches our configured domain
                if matched_email.endswith(f'@{email_domain}'):
                    logger.debug(f"Found alias in {header_name}: {matched_email}")
                    return matched_email
        
        logger.debug("No matching alias found in headers")
        return None
        
    except Exception as e:
        logger.error(f"Error extracting alias from headers: {e}")
        return None


def extract_raw_headers(msg: email.message.Message) -> str:
    """
    Extract raw email headers for debugging/logging purposes.
    """
    try:
        headers = []
        for key, value in msg.items():
            headers.append(f"{key}: {value}")
        return '\n'.join(headers)
    except Exception as e:
        logger.warning(f"Error extracting raw headers: {e}")
        return ''


def call_qwen_ai(prompt: str, email_body: str, subject: str = '') -> Dict[str, Any]:
    """Categorize an inbound email via the LLM.

    Migrated to use apps.ai.AIClient for PII tokenization, prompt fencing,
    and output validation. Returns a dict shaped for the existing parser
    in parse_ai_response.

    Signature kept identical to the original so all existing callers are unaffected.
    """
    from apps.ai.client import AIClient
    from apps.ai.schemas import EmailCategorization

    try:
        result = AIClient.complete(
            system_prompt=prompt,
            trusted=None,
            untrusted={
                "email_subject": subject,
                "email_body": email_body,
            },
            response_schema=EmailCategorization,
            call_site="email_categorizer",
            temperature=0.3,
            max_tokens=500,
        )
    except AIResponseValidationError as e:
        # Surface to caller in the same shape the existing parser expects on failure.
        return {"raw_response": e.raw_reply, "validation_failed": True}

    # Convert the typed object to the dict shape the existing parser handles.
    return {
        "summary": result.summary,
        "category": result.category,
        "action_required": result.action_required,
        "auto_resolvable": result.auto_resolvable,
    }


def call_qwen_ai_for_ticket_extraction(
    prompt: str,
    ticket_context: str,
    known_aliases: list[str] | None = None,
) -> dict:
    """Extract free-text claim fields from a Zendesk ticket description via LLM.

    The structured custom fields (name, email, phone, flight) are read directly
    from the ticket payload by the caller. The LLM is only responsible for
    interpreting the free-text description.
    """
    from apps.ai.client import AIClient
    from apps.ai.schemas import TicketExtraction
    from apps.ai.exceptions import AIResponseValidationError

    try:
        result = AIClient.complete(
            system_prompt=prompt,
            trusted=None,
            untrusted={"ticket_description": ticket_context},
            known_pii={"aliases": known_aliases or []},
            response_schema=TicketExtraction,
            call_site="zendesk_extractor",
            temperature=0.3,
            max_tokens=600,
        )
    except AIResponseValidationError as e:
        return {"raw_response": e.raw_reply, "validation_failed": True}

    return {
        "object_description": result.object_description or "",
        "additional_context": result.additional_context or "",
    }


def parse_ai_response(raw_response: str) -> Dict[str, Any]:
    """
    Parse the AI response to extract summary, category, action_required, auto_resolvable.
    Handles various JSON formats and provides fallback values.
    """
    result = {
        'summary': '',
        'category': 'UNKNOWN',
        'action_required': False,
        'auto_resolvable': False,
    }

    # Try to parse JSON from the response
    # Strategy 1: Try parsing the full response as JSON (handles nested objects)
    data = None
    try:
        data = json.loads(raw_response.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: Find JSON object in response text (handles markdown code blocks etc.)
    if data is None:
        # Match outermost braces, allowing nested braces
        json_match = re.search(r'\{(?:[^{}]|\{[^{}]*\})*\}', raw_response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
            except (json.JSONDecodeError, ValueError):
                pass

    if data is None:
        logger.warning(f"No JSON found in AI response: {raw_response[:100]}")
        return result

    try:

        # Extract summary
        for key in ['summary', 'Summary', 'AI_SUMMARY', 'ai_summary']:
            if key in data and data[key]:
                result['summary'] = str(data[key])[:1000]  # Limit length
                break

        # Extract category
        category_raw = ''
        for key in ['category', 'Category', 'CATEGORY']:
            if key in data and data[key]:
                category_raw = str(data[key]).strip().upper()
                break

        # Normalize category to valid choices
        valid_categories = [
            'OBJECT_FOUND', 'OBJECT_NOT_FOUND', 'RESUBMISSION_REQUIRED',
            'SUBMISSION_CONFIRMATION', 'GENERAL_CORRESPONDENCE', 'UNKNOWN'
        ]

        if category_raw in valid_categories:
            result['category'] = category_raw
        else:
            # Try to infer category from summary/text
            # IMPORTANT: Check "not found" BEFORE "found" to avoid misclassification
            raw_lower = raw_response.lower()
            if 'not found' in raw_lower or ('lost' in raw_lower and 'object' in raw_lower):
                result['category'] = 'OBJECT_NOT_FOUND'
            elif 'found' in raw_lower and 'object' in raw_lower:
                result['category'] = 'OBJECT_FOUND'
            elif 'resubmit' in raw_lower or 'additional information' in raw_lower:
                result['category'] = 'RESUBMISSION_REQUIRED'
            elif 'confirm' in raw_lower or 'submission' in raw_lower:
                result['category'] = 'SUBMISSION_CONFIRMATION'
            else:
                result['category'] = 'GENERAL_CORRESPONDENCE'

        # Extract action_required
        for key in ['action_required', 'actionRequired', 'action', 'Action_Required']:
            if key in data:
                value = data[key]
                if isinstance(value, bool):
                    result['action_required'] = value
                elif isinstance(value, str):
                    result['action_required'] = value.lower() in ['true', 'yes', '1']
                break

        # Extract auto_resolvable
        for key in ['auto_resolvable', 'autoResolvable', 'auto_resolved', 'Auto_Resolvable']:
            if key in data:
                value = data[key]
                if isinstance(value, bool):
                    result['auto_resolvable'] = value
                elif isinstance(value, str):
                    result['auto_resolvable'] = value.lower() in ['true', 'yes', '1']
                break

        # Auto-detect auto_resolvable based on category if not explicitly set
        if result['category'] in AUTO_RESOLVABLE_CATEGORIES and not result['action_required']:
            result['auto_resolvable'] = True

        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from AI response: {e}")
        logger.debug(f"Raw response: {raw_response}")

        # Fallback: try to infer from raw text
        raw_lower = raw_response.lower()

        # Infer category from text (check "not found" before "found")
        if 'not found' in raw_lower or ('lost' in raw_lower and 'object' in raw_lower):
            result['category'] = 'OBJECT_NOT_FOUND'
        elif 'found' in raw_lower and 'object' in raw_lower:
            result['category'] = 'OBJECT_FOUND'
        elif 'confirm' in raw_lower or 'submission' in raw_lower:
            result['category'] = 'SUBMISSION_CONFIRMATION'

        # Use first line as summary
        lines = raw_response.strip().split('\n')
        if lines:
            result['summary'] = lines[0][:500]

        return result


def mark_email_as_seen(imap_conn: imaplib.IMAP4_SSL, uid: str) -> bool:
    """
    Mark an email as SEEN (read) in IMAP.
    """
    try:
        imap_conn.store(uid, '+FLAGS', '\\Seen')
        logger.debug(f"Marked email {uid} as SEEN")
        return True
    except Exception as e:
        logger.error(f"Failed to mark email {uid} as SEEN: {e}")
        return False


def post_ai_summary_to_zendesk(
    zd_ticket_id: str,
    parsed: Dict[str, Any],
    subject: str,
    from_email: str,
    email_body: str,
    alias: str = '',
) -> bool:
    """
    Post full email body + AI analysis summary as internal note to Zendesk ticket.

    Args:
        zd_ticket_id: The Zendesk ticket ID
        parsed: Parsed AI analysis result
        subject: Original email subject
        from_email: Sender email address
        email_body: Full email body content
        alias: Matched email alias (if any)

    Returns:
        True if successful, False otherwise
    """
    if not zd_ticket_id:
        return False

    try:
        internal_note = (
            f"📧 **New Email Received**\n\n"
            f"**From:** {from_email}\n"
            f"**Subject:** {subject}\n"
            f"**Alias:** {alias}\n\n"
            f"---\n\n"
            f"**Original Message:**\n\n"
            f"{email_body}\n\n"
            f"---\n\n"
            f"**AI Analysis**\n\n"
            f"**Category:** {parsed['category']}\n\n"
            f"**Summary:** {parsed['summary']}\n\n"
            f"**Action Required:** {'Yes' if parsed['action_required'] else 'No'}\n\n"
            f"**Auto-Resolved:** {'Yes' if parsed.get('auto_resolvable', False) else 'No'}\n"
        )

        result = post_zendesk_comment(
            zd_ticket_id=zd_ticket_id,
            comment_body=internal_note,
            is_internal=True,  # Post as internal note
        )

        if result:
            logger.info(f"Posted email + AI summary to Zendesk ticket {zd_ticket_id}")
            return True
        else:
            logger.warning(f"Failed to post email + AI summary to Zendesk ticket {zd_ticket_id}")
            return False

    except Exception as e:
        logger.error(f"Error posting to Zendesk for ticket {zd_ticket_id}: {e}")
        return False


@transaction.atomic
def process_single_email(
    imap_conn: imaplib.IMAP4_SSL,
    uid: str,
    msg_data: bytes,
    ai_prompt: str,
    email_domain: str,
) -> Optional[EmailLog]:
    """
    Process a single email message with alias-based matching and AI categorization.
    
    Args:
        imap_conn: IMAP connection
        uid: Email UID
        msg_data: Raw email data
        ai_prompt: AI analysis prompt template
        email_domain: Configured email domain for alias matching
        
    Returns:
        The created EmailLog or None if skipped
    """
    try:
        # Parse the email
        msg = email.message_from_bytes(msg_data)

        # Dedup: an email is processed at most once, ever. Read flags can't
        # guarantee that (unresolved mail is left UNSEEN on purpose), the
        # Message-ID can.
        message_id = (msg.get('Message-ID') or '').strip()[:512]
        if message_id and EmailLog.objects.filter(message_id=message_id).exists():
            logger.info(f"Skipping already-processed email UID {uid} (Message-ID match)")
            return None

        # Extract sender email
        from_email = extract_from_email(msg)
        if not from_email:
            logger.warning(f"Could not extract from_email from message UID {uid}")
            return None

        # Extract alias from headers (To, Delivered-To, etc.)
        alias = extract_alias_from_headers(msg)

        # Initialize matching variables
        zd_ticket_id = ''
        claim = None
        matched_via = 'none'

        # Step 1: Try alias-based matching via Zendesk custom field
        if alias:
            logger.info(f"Attempting to match alias {alias} to Zendesk ticket")
            ticket_data = match_alias_to_zendesk_ticket(alias)

            if ticket_data:
                zd_ticket_id = str(ticket_data.get('id', ''))
                matched_via = 'alias'
                logger.info(f"✓ Matched alias {alias} to Zendesk ticket {zd_ticket_id}")

                # Try to find associated claim
                claim = Claim.objects.filter(zd_ticket_id=zd_ticket_id).first()
            else:
                logger.warning(f"✗ No Zendesk ticket found for alias {alias}")
        else:
            logger.warning(f"✗ No alias found in email headers")

        # Log matching result
        if zd_ticket_id:
            logger.info(f"Email will be posted to Zendesk ticket {zd_ticket_id}")
        else:
            logger.warning(f"Email will NOT be posted to Zendesk (no ticket match)")
        
        # Extract email body
        body = extract_email_body(msg)
        if not body:
            logger.warning(f"Empty body for message UID {uid}")
            body = '(No content extracted)'

        # Get subject
        subject = decode_mime_header(msg.get('Subject', '(No Subject)'))

        # Extract raw headers for debugging
        raw_headers = extract_raw_headers(msg)

        # Extract to_email and delivered_to from headers
        to_email = ''
        delivered_to = ''
        
        to_header = msg.get('To', '')
        if to_header:
            to_email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', decode_mime_header(to_header))
            if to_email_match:
                to_email = to_email_match.group(0).lower()
        
        delivered_to_header = msg.get('Delivered-To', '')
        if delivered_to_header:
            delivered_to_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', decode_mime_header(delivered_to_header))
            if delivered_to_match:
                delivered_to = delivered_to_match.group(0).lower()

        # Call Qwen AI for enhanced analysis
        ai_result = call_qwen_ai(ai_prompt, body, subject)
        if ai_result.get('validation_failed'):
            # Schema validation failed inside call_qwen_ai; fall back to old parser
            # against the raw LLM output as a last-ditch effort
            parsed = parse_ai_response(ai_result.get('raw_response', ''))
        else:
            # New AIClient path returned structured fields directly
            parsed = {
                'summary': ai_result.get('summary', ''),
                'category': ai_result.get('category', 'UNKNOWN'),
                'action_required': ai_result.get('action_required', False),
                'auto_resolvable': ai_result.get('auto_resolvable', False),
            }

        # Determine if email should be auto-resolved
        auto_resolved = False
        should_mark_as_seen = False

        if parsed.get('auto_resolvable', False) and parsed.get('category') in AUTO_RESOLVABLE_CATEGORIES:
            auto_resolved = True
            should_mark_as_seen = True
            logger.info(f"Email auto-resolved: category={parsed['category']}, UID={uid}")
        else:
            # Leave unread for agent attention
            logger.info(f"Email requires agent attention: category={parsed['category']}, UID={uid}")

        # Create EmailLog entry
        email_log = EmailLog.objects.create(
            claim=claim,
            subject=subject[:500],  # Limit length
            body=body,
            ai_summary=parsed['summary'],
            action_required=parsed['action_required'],
            from_email=from_email,
            to_email=to_email,
            delivered_to=delivered_to,
            alias_matched=alias or '',
            zd_ticket_id=zd_ticket_id,
            category=parsed['category'],
            auto_resolved=auto_resolved,
            raw_headers=raw_headers,
            message_id=message_id,
        )

        logger.info(
            f"Created EmailLog #{email_log.id} - matched_via={matched_via}, "
            f"zd_ticket={zd_ticket_id}, category={parsed['category']}, auto_resolved={auto_resolved}"
        )

        # Post full email + AI summary to Zendesk — only if matched via alias
        # (from_email fallback is unreliable and could post to wrong ticket)
        if zd_ticket_id and matched_via == 'alias':
            logger.info(f"Posting email to Zendesk ticket {zd_ticket_id}...")
            success = post_ai_summary_to_zendesk(
                zd_ticket_id=zd_ticket_id,
                parsed=parsed,
                subject=subject,
                from_email=from_email,
                email_body=body,
                alias=alias or '',
            )
            if success:
                logger.info(f"✓ Successfully posted email to Zendesk ticket {zd_ticket_id}")
            else:
                logger.error(f"✗ Failed to post email to Zendesk ticket {zd_ticket_id}")
        elif zd_ticket_id and matched_via != 'alias':
            logger.info(
                f"Skipping Zendesk posting for ticket {zd_ticket_id} — "
                f"matched via {matched_via}, not alias (risk of wrong ticket)"
            )
        else:
            logger.warning(f"Skipping Zendesk posting — no ticket ID or match")

        # Mark email as SEEN only if auto-resolved
        if should_mark_as_seen:
            mark_email_as_seen(imap_conn, uid)
        else:
            logger.debug(f"Leaving email UNSEEN for agent attention: UID={uid}")

        return email_log

    except AIResponseValidationError as e:
        # LLM output failed schema validation — flag for manual review
        # using EmailLog's existing `action_required` field (the project
        # does not have a separate llm_extraction_failed field on EmailLog;
        # that flag lives on Claim only).
        logger.warning(
            f"AI extraction failed for email UID {uid}: {e}. Flagged for manual review."
        )
        email_log = EmailLog.objects.create(
            subject=(subject if 'subject' in locals() else f'[Extraction failed UID {uid}]')[:500],
            body=body if 'body' in locals() else '',
            category='UNKNOWN',
            action_required=True,  # signals manual review
            auto_resolved=False,
            message_id=message_id if 'message_id' in locals() else '',
            # All other EmailLog fields have model-level defaults
        )
        return email_log
    except Exception as e:
        logger.error(f"Error processing email UID {uid}: {e}", exc_info=True)
        # Don't re-raise - continue with next email
        return None


def process_incoming_emails() -> Dict[str, Any]:
    """
    Main function to process incoming emails from IMAP.
    
    Phase 8: Smart Email Processing Overhaul
    
    Flow:
    1. Connect to IMAP using SystemSettings credentials
    2. Fetch UNSEEN emails (max 20)
    3. For each email:
       a) Extract from_email and alias from headers
       b) Match alias to Zendesk ticket (or fall back to from_email matching)
       c) Analyze with Qwen AI using email_analysis_prompt
       d) Create EmailLog with all fields (category, auto_resolved, etc.)
       e) Post AI summary to Zendesk ticket
       f) Auto-resolve if applicable (mark as SEEN), otherwise leave unread
    4. Return processing statistics

    Returns:
        Dict with processing statistics
    """
    stats = {
        'processed': 0,
        'matched': 0,
        'skipped_no_match': 0,
        'auto_resolved': 0,
        'requires_attention': 0,
        'errors': 0,
    }

    logger.info("Starting email processing (Phase 8: Smart Email Processing)...")

    # Load IMAP credentials and AI prompt from SystemSettings
    try:
        system_settings = SystemSettings.get_instance()
        imap_host = system_settings.imap_host
        imap_user = system_settings.imap_user
        imap_pass = system_settings.imap_pass
        ai_prompt = system_settings.email_analysis_prompt
        email_domain = system_settings.email_domain
    except Exception as e:
        logger.error(f"Failed to load SystemSettings: {e}")
        stats['errors'] += 1
        return stats

    # Validate credentials
    if not all([imap_host, imap_user, imap_pass]):
        logger.error("IMAP credentials not configured in SystemSettings")
        stats['errors'] += 1
        return stats

    # Validate email domain
    if not email_domain:
        logger.warning("Email domain not configured in SystemSettings - alias matching disabled")

    # Get configurable timeout
    imap_timeout = getattr(settings, 'IMAP_TIMEOUT', 30)

    imap_conn = None
    try:
        # Step 1: Connect to IMAP
        logger.info(f"Connecting to IMAP server: {imap_host}")
        imap_conn = imaplib.IMAP4_SSL(imap_host, timeout=imap_timeout)

        # Login
        imap_conn.login(imap_user, imap_pass)
        logger.info("IMAP login successful")

        # Select INBOX
        imap_conn.select('INBOX')
        logger.info("INBOX selected")

        # Step 2: Search for UNSEEN emails from the last EMAIL_LOOKBACK_DAYS
        # days only — the inbox holds years of mail; never sweep the backlog.
        status, messages = imap_conn.search(None, 'UNSEEN', 'SINCE', imap_since_date())

        if status != 'OK':
            logger.warning("Failed to search for UNSEEN emails")
            return stats

        # Parse UIDs
        uid_list = messages[0].split()
        if not uid_list:
            logger.info("No UNSEEN emails found")
            return stats

        # Limit to first 20 emails
        uid_list = uid_list[:MAX_EMAILS_PER_RUN]
        logger.info(f"Found {len(uid_list)} UNSEEN emails (processing up to {MAX_EMAILS_PER_RUN})")

        # Step 3: Process each email
        for uid in uid_list:
            stats['processed'] += 1
            uid_str = uid.decode('utf-8')

            try:
                # Fetch email by UID
                status, msg_data = imap_conn.fetch(uid, '(RFC822)')

                if status != 'OK':
                    logger.warning(f"Failed to fetch email UID {uid_str}")
                    stats['errors'] += 1
                    continue

                # Process the email
                result = process_single_email(
                    imap_conn,
                    uid_str,
                    msg_data[0][1],
                    ai_prompt,
                    email_domain,
                )

                if result:
                    stats['matched'] += 1
                    
                    if result.auto_resolved:
                        stats['auto_resolved'] += 1
                    else:
                        stats['requires_attention'] += 1
                else:
                    stats['skipped_no_match'] += 1

            except Exception as e:
                logger.error(f"Error processing email UID {uid_str}: {e}")
                stats['errors'] += 1
                continue

        logger.info(f"Email processing complete. Stats: {stats}")

    except Exception as e:
        # Catch all exceptions including IMAP errors
        logger.error(f"IMAP error during email processing: {e}")
        stats['errors'] += 1

    finally:
        # Clean up IMAP connection
        if imap_conn:
            try:
                imap_conn.close()
                imap_conn.logout()
                logger.info("IMAP connection closed")
            except Exception as e:
                logger.warning(f"Error closing IMAP connection: {e}")

    return stats


# ---------------------------------------------------------------------------
# Per-ticket email check (button-driven)
# ---------------------------------------------------------------------------
# Agents trigger this from the LORA claim page or the Zendesk sidebar Email
# tab. It only ever touches mail addressed to ONE ticket's alias: unread,
# from the last EMAIL_LOOKBACK_DAYS days, never processed before
# (Message-ID dedup). Everything else in the shared inbox stays exactly as
# it was — no reads, no flag changes, no logs, no notes.


def open_inbox() -> imaplib.IMAP4_SSL:
    """Connect, log in and select INBOX.

    Raises EmailNotConfigured when credentials are absent; IMAP errors
    propagate to the caller.
    """
    system_settings = SystemSettings.get_instance()
    if not all([system_settings.imap_host, system_settings.imap_user,
                system_settings.imap_pass]):
        raise EmailNotConfigured('IMAP credentials not configured in System settings')
    conn = imaplib.IMAP4_SSL(system_settings.imap_host,
                             timeout=getattr(settings, 'IMAP_TIMEOUT', 30))
    conn.login(system_settings.imap_user, system_settings.imap_pass)
    conn.select('INBOX')
    return conn


def search_alias_uids(conn: imaplib.IMAP4_SSL, alias: str) -> List[bytes]:
    """UIDs of unread mail from the window addressed to this alias.

    Checks both the To header and Delivered-To (alias delivery commonly
    surfaces only there); results are unioned.
    """
    since = imap_since_date()
    quoted = f'"{alias}"'
    uids = set()
    for criteria in (('TO', quoted), ('HEADER', 'Delivered-To', quoted)):
        status, messages = conn.search(None, 'UNSEEN', 'SINCE', since, *criteria)
        if status == 'OK' and messages and messages[0]:
            uids.update(messages[0].split())
    return sorted(uids, key=int)


def _ai_tags_for(category: str, action_required: bool) -> set:
    tags = set()
    if category in AI_TAG_BY_CATEGORY:
        tags.add(AI_TAG_BY_CATEGORY[category])
    if action_required:
        tags.add(AI_TAG_ATTENTION)
    return tags


def _first_email_in_header(msg: email.message.Message, header_name: str) -> str:
    value = decode_mime_header(msg.get(header_name, '') or '')
    match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', value)
    return match.group(0).lower() if match else ''


def _process_ticket_email(
    conn: imaplib.IMAP4_SSL,
    uid: str,
    msg: email.message.Message,
    message_id: str,
    ticket_id: str,
    claim,
    alias: str,
    ai_prompt: str,
) -> Dict[str, Any]:
    """Process one email already known to belong to ticket_id: AI
    categorization, EmailLog, internal note. Returns a UI-friendly entry."""
    from_email = extract_from_email(msg) or ''
    subject = decode_mime_header(msg.get('Subject', '(No Subject)'))
    body = extract_email_body(msg) or '(No content extracted)'

    ai_result = call_qwen_ai(ai_prompt, body, subject)
    if ai_result.get('validation_failed'):
        parsed = parse_ai_response(ai_result.get('raw_response', ''))
    else:
        parsed = ai_result

    auto_resolved = (bool(parsed.get('auto_resolvable'))
                     and parsed.get('category') in AUTO_RESOLVABLE_CATEGORIES)

    email_log = EmailLog.objects.create(
        claim=claim,
        subject=subject[:500],
        body=body,
        ai_summary=parsed.get('summary', ''),
        action_required=bool(parsed.get('action_required')),
        from_email=from_email,
        to_email=_first_email_in_header(msg, 'To'),
        delivered_to=_first_email_in_header(msg, 'Delivered-To'),
        alias_matched=alias,
        zd_ticket_id=str(ticket_id),
        category=parsed.get('category', 'UNKNOWN'),
        auto_resolved=auto_resolved,
        raw_headers=extract_raw_headers(msg),
        message_id=message_id,
    )

    note_posted = post_ai_summary_to_zendesk(
        zd_ticket_id=str(ticket_id),
        parsed=parsed,
        subject=subject,
        from_email=from_email,
        email_body=body,
        alias=alias,
    )

    # Same inbox contract as the global flow: routine mail is marked read,
    # anything needing a human stays unread for agent attention.
    if auto_resolved:
        mark_email_as_seen(conn, uid)

    logger.info(
        f"Email check: EmailLog #{email_log.id} for ticket {ticket_id} — "
        f"category={email_log.category}, auto_resolved={auto_resolved}, "
        f"note_posted={note_posted}"
    )
    return {
        'email_log_id': email_log.id,
        'subject': subject[:200],
        'from_email': from_email,
        'category': parsed.get('category', 'UNKNOWN'),
        'summary': parsed.get('summary', ''),
        'action_required': bool(parsed.get('action_required')),
        'auto_resolved': auto_resolved,
        'note_posted': note_posted,
    }


def check_email_for_ticket(ticket_id: str, claim, alias: str) -> Dict[str, Any]:
    """Check the mailbox for new mail addressed to one ticket's alias.

    Each new email gets: AI categorization, an EmailLog row (linked to the
    claim when there is one), an internal note on the ticket, and — per
    category — additive ai_* tags on the ticket.

    Raises EmailNotConfigured when IMAP credentials are missing. IMAP
    connection errors propagate; per-email failures are counted, not raised.
    """
    if not re.fullmatch(r'[\w.+-]+@[\w-]+(\.[\w-]+)+', alias or ''):
        raise InvalidAlias(f"Alias {alias!r} doesn't look like an email address")

    results = {
        'alias': alias,
        'found': 0,
        'processed': [],
        'already_processed': 0,
        'tags_added': [],
        'errors': 0,
        'capped': False,
    }
    ai_prompt = SystemSettings.get_instance().email_analysis_prompt
    conn = open_inbox()
    try:
        uids = search_alias_uids(conn, alias)
        results['found'] = len(uids)
        tags = set()
        processed_count = 0
        for uid in uids:
            if processed_count >= MAX_EMAILS_PER_RUN:
                # Cap counts AI-processed emails only — dedup skips are free,
                # so a re-click continues where this run stopped.
                results['capped'] = True
                break
            uid_str = uid.decode('utf-8') if isinstance(uid, bytes) else str(uid)
            try:
                status, msg_data = conn.fetch(uid, '(RFC822)')
                if status != 'OK' or not msg_data or msg_data[0] is None:
                    logger.warning(f"Email check: failed to fetch UID {uid_str}")
                    results['errors'] += 1
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                message_id = (msg.get('Message-ID') or '').strip()[:512]
                if message_id and EmailLog.objects.filter(message_id=message_id).exists():
                    results['already_processed'] += 1
                    continue
                entry = _process_ticket_email(
                    conn, uid_str, msg, message_id, ticket_id, claim, alias, ai_prompt)
                results['processed'].append(entry)
                tags.update(_ai_tags_for(entry['category'], entry['action_required']))
                processed_count += 1
            except IntegrityError:
                # A simultaneous press on the same ticket won the race — the
                # unique message_id constraint bounced this copy before any
                # note was posted. Same outcome as the dedup check.
                results['already_processed'] += 1
            except Exception as e:
                logger.error(
                    f"Email check: error on UID {uid_str} for ticket {ticket_id}: {e}",
                    exc_info=True)
                results['errors'] += 1
        if tags and add_zendesk_ticket_tags(str(ticket_id), sorted(tags)):
            results['tags_added'] = sorted(tags)
    finally:
        try:
            conn.close()
            conn.logout()
        except Exception:
            pass
    return results
