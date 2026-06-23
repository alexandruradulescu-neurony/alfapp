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
from typing import Optional, Dict, Any, List

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from apps.claims.models import Claim
from apps.config.encrypted_fields import is_decryption_failure
from apps.config.models import SystemSettings
from apps.communications.models import EmailLog
from apps.integrations.services import (
    add_zendesk_ticket_tags,
    match_alias_to_zendesk_ticket,
    post_zendesk_comment,
)
from apps.ai.exceptions import AIResponseValidationError
from apps.communications.constants import (
    DEFAULT_IMAP_TIMEOUT,
    EMAIL_LOOKBACK_DAYS,
    MAX_EMAILS_PER_RUN,
)

logger = logging.getLogger(__name__)

# How far back any mailbox read ever looks. The inbox holds years of mail;
# LORA's window is always the last EMAIL_LOOKBACK_DAYS days (apps.communications.constants),
# read or unread state untouched beyond it.

# IMAP SINCE wants RFC 3501 dates (e.g. 10-Jun-2026) with fixed English
# month names — strftime('%b') is locale-dependent, so spell them out.
IMAP_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

# Categories that can be auto-resolved
AUTO_RESOLVABLE_CATEGORIES = [
    EmailLog.CATEGORY_SUBMISSION_CONFIRMATION,
    EmailLog.CATEGORY_OBJECT_NOT_FOUND,
]

# Category → AI-added Zendesk tag. Routine categories (submission
# confirmations, general correspondence, unknown) are deliberately untagged.
AI_TAG_BY_CATEGORY = {
    EmailLog.CATEGORY_OBJECT_FOUND: 'ai_object_found',
    EmailLog.CATEGORY_OBJECT_NOT_FOUND: 'ai_object_not_found',
    EmailLog.CATEGORY_SHIPPING_INFORMATION: 'ai_shipping_information',
    EmailLog.CATEGORY_RESUBMISSION_REQUIRED: 'ai_resubmission_required',
}
# Added whenever the AI says the email needs a human, regardless of category.
AI_TAG_ATTENTION = 'ai_attention_needed'

# Authoritative category definitions appended to the (non-user-editable) analysis
# prompt at call time, so the taxonomy is always current in code — no prompt migration
# needed. The schema (apps/ai/schemas.py) enforces the allowed values; this tells the
# model HOW to choose, and ensures shipping/tracking mail stops landing in
# GENERAL_CORRESPONDENCE.
EMAIL_CATEGORY_GUIDE = """Classify the email into EXACTLY ONE category (this list is authoritative):
- OBJECT_FOUND: the institution has located or identified the lost item.
- OBJECT_NOT_FOUND: the institution searched and did not find it, or is closing the search.
- SHIPPING_INFORMATION: the item is being returned or shipped to the client — a tracking number, courier/carrier name, shipping label, dispatch or out-for-delivery/delivered notice, or "your item is on its way". Choose this whenever the email carries shipping or tracking details, even if it also mentions the item was found. This is an IMPORTANT category: set action_required=true and auto_resolvable=false.
- RESUBMISSION_REQUIRED: the institution needs more detail or a corrected/re-filed report.
- SUBMISSION_CONFIRMATION: an automated receipt acknowledging a report was filed (no new information).
- GENERAL_CORRESPONDENCE: relevant correspondence that fits none of the above.
- UNKNOWN: the content cannot be determined."""


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


_HTML_TAG_RE = re.compile(r'<[a-zA-Z!/][^>]*>')


def _looks_like_html(text: str) -> bool:
    """True if a supposedly-plain body actually carries HTML markup — some senders
    (and alias forwarders) put HTML in the text/plain slot."""
    return bool(_HTML_TAG_RE.search(text or ''))


def _inline_link(match) -> str:
    """<a href=URL>label</a> -> 'label (URL)' so the link target survives in text."""
    url = match.group(1).strip()
    label = re.sub(r'(?s)<[^>]+>', '', match.group(2)).strip()
    if not label or label == url:
        return f' {url} '
    return f'{label} ({url})'


def _inline_img(match) -> str:
    """<img src=URL alt=ALT> -> '[image: ALT - URL]' so photos aren't silently lost."""
    tag = match.group(0)
    src = re.search(r'(?i)\bsrc=["\']([^"\']+)["\']', tag)
    if not src:
        return ''
    alt = re.search(r'(?i)\balt=["\']([^"\']*)["\']', tag)
    alt_text = alt.group(1).strip() if alt else ''
    return f'[image: {alt_text + " - " if alt_text else ""}{src.group(1).strip()}]'


def _html_to_text(html: str) -> str:
    """Best-effort HTML -> readable plain text, for the body we store, show in LORA,
    and feed the AI. Keeps link targets and image addresses inline (nothing silently
    lost), drops style/script blocks, turns <br> and block ends into line breaks,
    strips the remaining tags, unescapes entities, and collapses whitespace."""
    from html import unescape
    if not html:
        return ''
    text = re.sub(r'(?is)<(script|style)\b.*?</\1\s*>', '', html)   # drop css/js blocks
    text = re.sub(r'(?is)<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                  _inline_link, text)                              # keep link URLs
    text = re.sub(r'(?is)<img\b[^>]*>', _inline_img, text)         # keep image URLs
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)                     # <br> -> newline
    text = re.sub(r'(?i)</(p|div|tr|li|h[1-6]|table|ul|ol)\s*>', '\n', text)  # block ends
    text = re.sub(r'(?s)<[^>]+>', '', text)                        # remaining tags
    text = unescape(text).replace(' ', ' ')                  # entities + nbsp
    text = re.sub(r'[ \t]+', ' ', text)                           # collapse spaces
    text = re.sub(r'\n[ \t]+', '\n', text)                        # trim line starts
    text = re.sub(r'\n{3,}', '\n\n', text)                        # collapse blank runs
    return text.strip()


def extract_email_body(msg: email.message.Message) -> str:
    """
    Extract a readable plain-text body from an email message.
    Prefers a genuine text/plain part; otherwise converts the text/html part to
    text. If the "plain" part actually contains HTML, it is cleaned too.
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
                logger.warning("Error decoding email part: %s", e)
                continue
    else:
        # Non-multipart: route by the part's own content type, so an HTML-only
        # email isn't dumped verbatim into the plain-text slot.
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                content = payload.decode(charset, errors='replace')
                if msg.get_content_type() == 'text/html':
                    body_html = content
                else:
                    body_text = content
        except Exception as e:
            logger.warning("Error decoding email payload: %s", e)

    # A genuine, non-empty plain-text part wins. Strip FIRST: a whitespace-only
    # text/plain alternative (Chargerback sends "=20" — a lone space) must NOT shadow
    # the real HTML and then trim down to nothing. Otherwise convert whatever HTML we
    # have — a real text/html part, OR markup stuffed into the text/plain slot — into
    # readable text (keeping link and image addresses inline).
    body_text = (body_text or '').strip()
    if body_text and not _looks_like_html(body_text):
        return body_text
    html_source = body_html or body_text
    if html_source:
        return _html_to_text(html_source)
    return ''


def extract_email_html(msg: email.message.Message) -> str:
    """Return the email's HTML — the text/html part, or a text/plain part that
    actually carries HTML markup. '' when the email is genuinely plain text. Used
    to render the original email faithfully in the Zendesk note (links + images)."""
    html_part = ''
    plain_part = ''
    if msg.is_multipart():
        for part in msg.walk():
            if 'attachment' in str(part.get('Content-Disposition') or ''):
                continue
            ctype = part.get_content_type()
            if ctype not in ('text/html', 'text/plain'):
                continue
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                content = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
            except Exception as e:
                logger.warning("Error decoding part for HTML extract: %s", e)
                continue
            if ctype == 'text/html':
                html_part = content
            else:
                plain_part = content
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                content = payload.decode(msg.get_content_charset() or 'utf-8', errors='replace')
                if msg.get_content_type() == 'text/html':
                    html_part = content
                else:
                    plain_part = content
        except Exception as e:
            logger.warning("Error decoding payload for HTML extract: %s", e)
    if html_part:
        return html_part
    if plain_part and _looks_like_html(plain_part):
        return plain_part
    return ''


# Tags/attributes kept when rendering the original email inside a Zendesk internal
# note. Links, images, tables and basic formatting survive; scripts, styles and
# event handlers are dropped (Zendesk re-sanitizes html_body server-side too).
_EMAIL_NOTE_TAGS = [
    'p', 'br', 'div', 'span', 'a', 'img', 'b', 'strong', 'i', 'em', 'u', 's',
    'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tr', 'td', 'th',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'hr', 'pre', 'small', 'font',
]
_EMAIL_NOTE_ATTRS = {
    'a': ['href', 'title', 'target', 'rel'],
    'img': ['src', 'alt', 'width', 'height'],
}


def _sanitize_email_html(html: str) -> str:
    """Strip executable/unsafe content from the email HTML before it goes into the
    note. Drops <script>/<style> blocks and any disallowed tags; keeps links,
    images, tables and basic formatting."""
    import bleach
    cleaned = re.sub(r'(?is)<(script|style)\b.*?</\1\s*>', '', html or '')
    return bleach.clean(cleaned, tags=_EMAIL_NOTE_TAGS, attributes=_EMAIL_NOTE_ATTRS,
                        protocols=['http', 'https', 'mailto', 'cid'], strip=True)


def _build_email_note_html(parsed: Dict[str, Any], subject: str, from_email: str,
                           alias: str, email_html: str) -> str:
    """The internal-note HTML: our header / AI-analysis chrome wrapped around the
    sanitized original email, so links and inline images render in the ticket."""
    from django.utils.html import escape
    safe_email = _sanitize_email_html(email_html)
    action = 'Yes' if parsed.get('action_required') else 'No'
    auto = 'Yes' if parsed.get('auto_resolvable', False) else 'No'
    return (
        '<p>\U0001F4E7 <strong>New Email Received</strong></p>'
        f'<p><strong>From:</strong> {escape(from_email)}<br>'
        f'<strong>Subject:</strong> {escape(subject)}<br>'
        f'<strong>Alias:</strong> {escape(alias)}</p>'
        '<hr>'
        '<p><strong>Original Message:</strong></p>'
        f'<blockquote>{safe_email}</blockquote>'
        '<hr>'
        '<p><strong>AI Analysis</strong><br>'
        f'<strong>Category:</strong> {escape(str(parsed.get("category", "")))}<br>'
        f'<strong>Summary:</strong> {escape(str(parsed.get("summary", "")))}<br>'
        f'<strong>Action Required:</strong> {action}<br>'
        f'<strong>Auto-Resolved:</strong> {auto}</p>'
    )


# AnonAddy/addy.io forwards mail through per-ticket aliases and rewrites the
# From into an encoded alias address (alias+real.local=real.domain@aliasdomain),
# but preserves the true sender verbatim in these headers. Prefer them.
ANONADDY_ORIGINAL_SENDER_HEADERS = (
    'X-AnonAddy-Original-Sender',
    'X-AnonAddy-Original-Envelope-From',
    'X-Original-Sender',
)
_EMAIL_RE = re.compile(r'[\w.+=-]+@[\w.-]+\.\w+')
_PLAIN_EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.\w+')

# Alias-validation pattern — the SINGLE source of truth for the IMAP-interpolation
# guard. An alias is interpolated into IMAP search commands (search_alias_uids),
# so it MUST look like a plain email address with no quotes/whitespace/control
# chars that could break out of the quoted search term. Stricter than the
# extraction regexes above (anchored, fullmatch) because here we are validating a
# whole field, not pulling an address out of free text.
_ALIAS_RE = re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+')


def _validate_alias(alias: str) -> str:
    """Return the alias unchanged if it is a syntactically valid email address,
    else raise InvalidAlias. Every IMAP path that interpolates the alias into a
    search command guards through here, so the safety contract (see InvalidAlias)
    cannot be bypassed by reaching search_alias_uids directly."""
    if not _ALIAS_RE.fullmatch(alias or ''):
        raise InvalidAlias(f"Alias {alias!r} doesn't look like an email address")
    return alias


def _is_duplicate_message(message_id: str) -> bool:
    """True if an EmailLog already exists for this RFC 5322 Message-ID — the
    process-at-most-once dedup key (see EmailLog.message_id). Blank ids (old rows
    / messages with no Message-ID) never count as duplicates. The unique
    constraint is the real guard against same-second races; this is the cheap
    pre-check both inbound flows share."""
    return bool(message_id) and EmailLog.objects.filter(message_id=message_id).exists()


def decode_alias_encoded_address(addr: str) -> str:
    """Recover the real sender from an AnonAddy-encoded alias address.

    'andrei.deaconu+alexandru.radulescu=neurony.ro@mailapptoday.com'
        -> 'alexandru.radulescu@neurony.ro'
    The contact is the segment after the last '+' in the local part, with the
    last '=' standing in for '@'. Returns '' if the address isn't encoded.
    """
    local, _, _domain = addr.rpartition('@')
    if '=' not in local:
        return ''
    contact = local.split('+')[-1]  # drop an optional 'alias+' prefix
    if '=' not in contact:
        return ''
    real_local, _, real_domain = contact.rpartition('=')
    if real_local and '.' in real_domain:
        return f'{real_local}@{real_domain}'.lower()
    return ''


def extract_from_email(msg: email.message.Message) -> Optional[str]:
    """Extract the TRUE sender's email address.

    For aliased/forwarded mail (AnonAddy unified inbox), the From header is an
    encoded alias — the real sender lives in X-AnonAddy-Original-Sender. We
    prefer that header, then fall back to decoding the encoded From, then to
    the raw From address.
    """
    # 1. Trust AnonAddy's original-sender headers when present.
    for header_name in ANONADDY_ORIGINAL_SENDER_HEADERS:
        value = decode_mime_header(msg.get(header_name, '') or '')
        match = _PLAIN_EMAIL_RE.search(value)
        if match:
            return match.group(0).lower()

    from_header = msg.get('From', '')
    if not from_header:
        return None
    from_header = decode_mime_header(from_header)

    # 2. Capture the full address (including + and = of an encoded alias) and,
    #    if it is alias-encoded, decode it back to the real sender.
    match = _EMAIL_RE.search(from_header)
    if match:
        addr = match.group(0).lower()
        return decode_alias_encoded_address(addr) or addr

    # 3. Last resort: the header is bare text containing an address.
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
            # Extract the first address from the header (single source of truth
            # for the email regex), then check it against the configured domain.
            matched_email = _first_email_in_header(msg, header_name)
            if matched_email and matched_email.endswith(f'@{email_domain}'):
                logger.debug("Found alias in %s: %s", header_name, matched_email)
                return matched_email
        
        logger.debug("No matching alias found in headers")
        return None

    except Exception as e:
        logger.error("Error extracting alias from headers: %s", e)
        return None


_ALIAS_CANDIDATE_HEADERS = ('Delivered-To', 'X-Original-To', 'X-AnonAddy-Original-To',
                             'To', 'X-RCPT-TO', 'Cc')


def extract_recipient_candidates(msg) -> list:
    """Every recipient address from the headers an alias can appear in — deduped,
    lowercased, ANY domain. Used to match an inbound email to its ticket by the
    'Email used for submissions' field, with no configured alias domain required."""
    seen = []
    for header_name in _ALIAS_CANDIDATE_HEADERS:
        for raw in msg.get_all(header_name, []) or []:
            for addr in _PLAIN_EMAIL_RE.findall(str(raw)):
                a = addr.strip().lower()
                if a and a not in seen:
                    seen.append(a)
    return seen


def find_zendesk_ticket_for_email(msg):
    """Match an inbound email to a Zendesk ticket by its 'Email used for submissions'
    field, trying each recipient address (any domain). Returns (ticket_data, alias)
    or (None, '')."""
    from apps.integrations.services import match_alias_to_zendesk_ticket
    for addr in extract_recipient_candidates(msg):
        ticket = match_alias_to_zendesk_ticket(addr)
        if ticket:
            return ticket, addr
    return None, ''


def recover_orphan_emails(dry_run: bool = False) -> dict:
    """Re-route orphan EmailLogs (no zd_ticket_id) to their tickets using the now
    domain-agnostic matching and the STORED analysis (no IMAP re-fetch, no new AI).
    Idempotent. Returns {'matched': n, 'dry_run': bool}."""
    import email as email_lib
    from apps.claims.models import Claim
    from apps.integrations.services import add_zendesk_ticket_tags, import_claim_from_zendesk_ticket
    matched = 0
    orphans = EmailLog.objects.filter(zd_ticket_id__in=['', None]).exclude(raw_headers='')
    for el in orphans:
        try:
            msg = email_lib.message_from_string(el.raw_headers)
        except Exception:
            continue
        ticket, alias = find_zendesk_ticket_for_email(msg)
        if not ticket:
            continue
        zd_ticket_id = str(ticket.get('id', ''))
        matched += 1
        if dry_run:
            continue
        claim = Claim.objects.filter(zd_ticket_id=zd_ticket_id).first()
        if claim is None and getattr(SystemSettings.get_instance(), 'import_claims_from_email', False):
            imported, _created = import_claim_from_zendesk_ticket(zd_ticket_id)
            claim = imported or claim
        el.zd_ticket_id = zd_ticket_id
        el.claim = claim
        el.alias_matched = alias
        # updated_at is auto_now=True — Django updates it automatically on save
        el.save(update_fields=['zd_ticket_id', 'claim', 'alias_matched'])
        parsed = {'category': el.category, 'summary': el.ai_summary,
                  'action_required': el.action_required, 'auto_resolvable': el.auto_resolved}
        post_ai_summary_to_zendesk(zd_ticket_id=zd_ticket_id, parsed=parsed, subject=el.subject,
                                   from_email=el.from_email, email_body=el.body, alias=alias)
        tags = _ai_tags_for(el.category, el.action_required)
        if tags:
            add_zendesk_ticket_tags(zd_ticket_id, sorted(tags))
    return {'matched': matched, 'dry_run': dry_run}


# Bodies LORA stores when extraction found nothing — the rows worth re-fetching.
_EMPTY_BODY_VALUES = ['', '(No content extracted)']


def fetch_raw_by_message_id(conn: imaplib.IMAP4_SSL, message_id: str) -> Optional[bytes]:
    """Fetch a message's full raw bytes from the mailbox by its Message-ID, or None if
    it is no longer there. Searches the whole mailbox (seen + unseen, any date) and
    does not change the read flag (BODY.PEEK)."""
    mid = (message_id or '').strip()
    if not mid:
        logger.info("backfill fetch: empty Message-ID, skipping")
        return None
    # The Message-ID (<...@...>) MUST be a quoted IMAP string — its < @ > are not valid
    # bare-atom characters. Servers vary on HEADER search, so try a few forms and LOG
    # each so a miss is diagnosable (this was previously a silent 0/21).
    bare = mid.strip('<>').replace('"', '\\"')
    attempts = [
        ('header', ('HEADER', 'Message-ID', '"%s"' % mid.replace('"', '\\"'))),
        ('header-bare', ('HEADER', 'Message-ID', '"%s"' % bare)),
        # Full-text search — servers that won't resolve a HEADER lookup on a huge mailbox
        # often still match the Message-ID string in the message text.
        ('text', ('TEXT', '"%s"' % mid.replace('"', '\\"'))),
        ('text-bare', ('TEXT', '"%s"' % bare)),
    ]
    for label, criteria in attempts:
        try:
            status, data = conn.search(None, *criteria)
            seq_nums = data[0].split() if (data and data[0]) else []
            logger.info("backfill fetch [%s] %s -> status=%s matches=%d",
                        label, mid[:70], status, len(seq_nums))
            if status != 'OK' or not seq_nums:
                continue
            status, msg_data = conn.fetch(seq_nums[-1], '(BODY.PEEK[])')
            raw = (msg_data[0][1] if (msg_data and isinstance(msg_data[0], tuple)
                                      and len(msg_data[0]) > 1) else None)
            logger.info("backfill fetch [%s] fetch seq=%s -> status=%s bytes=%d",
                        label, seq_nums[-1], status, len(raw) if raw else 0)
            if status == 'OK' and raw:
                return raw
        except Exception as e:
            logger.warning("backfill fetch [%s] Message-ID %s FAILED: %r", label, mid[:70], e)
    return None


def reprocess_email_logs(*, dry_run: bool = False, limit: Optional[int] = None,
                         claim_id: Optional[int] = None) -> dict:
    """Backfill existing emails: (1) recover empty bodies by re-fetching the original
    from the mailbox by Message-ID and re-extracting with the current logic, and
    (2) re-categorize the 'suspect' set — empty-body rows plus those still tagged
    GENERAL_CORRESPONDENCE or UNKNOWN — with the current, shipping-aware categorizer,
    then re-apply Zendesk tags. It only ever re-runs the suspect set, so a meaningful
    category (Object Found/Not Found/Resubmission/Shipping) is never clobbered.
    Idempotent. Returns a summary dict."""
    from django.db.models import Q
    from apps.integrations.services import add_zendesk_ticket_tags

    suspect = (Q(body__in=_EMPTY_BODY_VALUES)
               | Q(category__in=[EmailLog.CATEGORY_GENERAL_CORRESPONDENCE,
                                 EmailLog.CATEGORY_UNKNOWN]))
    qs = EmailLog.objects.filter(suspect)
    if claim_id:
        qs = qs.filter(claim_id=claim_id)
    qs = qs.order_by('-id')
    if limit:
        qs = qs[:limit]

    summary = {'examined': 0, 'body_recovered': 0, 'body_unrecoverable': 0,
               'recategorized': 0, 'retagged': 0, 'dry_run': dry_run}
    if dry_run:
        summary['examined'] = qs.count()
        summary['would_refetch'] = qs.filter(body__in=_EMPTY_BODY_VALUES).count()
        return summary

    ai_prompt = SystemSettings.get_instance().email_analysis_prompt
    conn = None
    try:
        for el in qs:
            summary['examined'] += 1
            body = el.body
            if body in _EMPTY_BODY_VALUES:
                raw = None
                if el.message_id:
                    if conn is None:
                        conn = open_inbox()
                        try:
                            typ, cnt = conn.select('INBOX')
                            logger.info("backfill: INBOX message_count=%s (status=%s)", cnt, typ)
                        except Exception as e:
                            logger.info("backfill: INBOX count probe skipped: %r", e)
                    raw = fetch_raw_by_message_id(conn, el.message_id)
                else:
                    logger.info("backfill: EmailLog #%s has no Message-ID", el.id)
                body = extract_email_body(email.message_from_bytes(raw)) if raw else ''
                if not body:
                    logger.info("backfill: EmailLog #%s UNRECOVERABLE (had_raw=%s)", el.id, bool(raw))
                    summary['body_unrecoverable'] += 1
                    continue
                el.body = body
                summary['body_recovered'] += 1
                logger.info("backfill: EmailLog #%s body recovered, %d chars", el.id, len(body))
            old_category = el.category
            ai = call_qwen_ai(ai_prompt, body, el.subject,
                              known_pii=_known_pii_for_email(el.claim))
            if not ai.get('validation_failed'):
                el.category = ai.get('category') or old_category
                el.ai_summary = ai.get('summary') or el.ai_summary
                el.action_required = ai.get('action_required', el.action_required)
            el.save(update_fields=['body', 'category', 'ai_summary', 'action_required'])
            if el.category != old_category:
                summary['recategorized'] += 1
            if el.zd_ticket_id:
                tags = _ai_tags_for(el.category, el.action_required)
                if tags:
                    try:
                        add_zendesk_ticket_tags(el.zd_ticket_id, sorted(tags))
                        summary['retagged'] += 1
                    except Exception as e:
                        logger.warning("Re-tag failed for ticket %s: %s", el.zd_ticket_id, e)
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
    return summary


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
        logger.warning("Error extracting raw headers: %s", e)
        return ''


def _known_pii_for_email(claim: Optional[Claim]) -> Optional[Dict[str, Any]]:
    """Client PII (name + address + contact handles) to tokenize before the
    categorizer reaches the LLM provider. Institution replies routinely quote
    the client's name/address, and the LLM provider sits OUTSIDE the trust
    boundary — the regex tokenizer alone can't catch a free-text name. Returns
    None when no claim is linked (claimless mail has no known client identity)."""
    if not claim:
        return None
    from apps.communications.client_report import _known_pii_for
    return _known_pii_for(claim)


def call_qwen_ai(prompt: str, email_body: str, subject: str = '',
                 known_pii: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Categorize an inbound email via the LLM.

    Migrated to use apps.ai.AIClient for PII tokenization, prompt fencing,
    and output validation. Returns a dict shaped for the existing parser
    in parse_ai_response.

    known_pii: client identifiers (name/address/contacts) to mask before the
    provider sees the body — pass it whenever a claim is known.
    """
    from apps.ai.client import AIClient
    from apps.ai.schemas import EmailCategorization

    try:
        result = AIClient.complete(
            system_prompt=f"{prompt}\n\n{EMAIL_CATEGORY_GUIDE}",
            trusted=None,
            untrusted={
                "email_subject": subject,
                "email_body": email_body,
            },
            known_pii=known_pii,
            response_schema=EmailCategorization,
            call_site="email_categorizer",
            temperature=0.3,
            max_tokens=4096,
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

    try:
        result = AIClient.complete(
            system_prompt=prompt,
            trusted=None,
            untrusted={"ticket_description": ticket_context},
            known_pii={"aliases": known_aliases or []},
            response_schema=TicketExtraction,
            call_site="zendesk_extractor",
            temperature=0.3,
            max_tokens=4096,
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
        'category': EmailLog.CATEGORY_UNKNOWN,
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
        logger.warning("No JSON found in AI response: %s", raw_response[:100])
        return result
    if not isinstance(data, dict):
        # Valid JSON but an array/scalar — `key in data` / `data[key]` below would
        # raise TypeError; degrade to the default result instead of crashing.
        logger.warning("AI response JSON is not an object: %s", type(data).__name__)
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

        # Normalize category to valid choices (derive from the model so a new
        # category can't drift out of sync with EmailLog.CATEGORY_CHOICES).
        valid_categories = [c[0] for c in EmailLog.CATEGORY_CHOICES]

        if category_raw in valid_categories:
            result['category'] = category_raw
        else:
            # Try to infer category from summary/text
            # IMPORTANT: Check "not found" BEFORE "found" to avoid misclassification
            raw_lower = raw_response.lower()
            if 'not found' in raw_lower or ('lost' in raw_lower and 'object' in raw_lower):
                result['category'] = EmailLog.CATEGORY_OBJECT_NOT_FOUND
            elif 'found' in raw_lower and 'object' in raw_lower:
                result['category'] = EmailLog.CATEGORY_OBJECT_FOUND
            elif 'resubmit' in raw_lower or 'additional information' in raw_lower:
                result['category'] = EmailLog.CATEGORY_RESUBMISSION_REQUIRED
            elif 'confirm' in raw_lower or 'submission' in raw_lower:
                result['category'] = EmailLog.CATEGORY_SUBMISSION_CONFIRMATION
            else:
                result['category'] = EmailLog.CATEGORY_GENERAL_CORRESPONDENCE

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

    except (TypeError, KeyError, AttributeError) as e:
        # JSON was already parsed above, so the real risk here is an unexpectedly
        # shaped field — degrade gracefully rather than 500 the email pipeline.
        logger.error("Malformed field in AI response: %s", e)
        logger.debug("Raw response: %s", raw_response)

        # Fallback: try to infer from raw text
        raw_lower = raw_response.lower()

        # Infer category from text (check "not found" before "found")
        if 'not found' in raw_lower or ('lost' in raw_lower and 'object' in raw_lower):
            result['category'] = EmailLog.CATEGORY_OBJECT_NOT_FOUND
        elif 'found' in raw_lower and 'object' in raw_lower:
            result['category'] = EmailLog.CATEGORY_OBJECT_FOUND
        elif 'confirm' in raw_lower or 'submission' in raw_lower:
            result['category'] = EmailLog.CATEGORY_SUBMISSION_CONFIRMATION

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
        logger.debug("Marked email %s as SEEN", uid)
        return True
    except Exception as e:
        logger.error("Failed to mark email %s as SEEN: %s", uid, e)
        return False


def post_ai_summary_to_zendesk(
    zd_ticket_id: str,
    parsed: Dict[str, Any],
    subject: str,
    from_email: str,
    email_body: str,
    alias: str = '',
    email_html: str = '',
) -> bool:
    """
    Post the original email + AI analysis as an internal note on the Zendesk ticket.

    When email_html is provided, the note is posted as rendered HTML (html_body) so
    the original email's links and inline images show in the ticket; otherwise it
    falls back to the plain-text note built from email_body.

    Args:
        zd_ticket_id: The Zendesk ticket ID
        parsed: Parsed AI analysis result
        subject: Original email subject
        from_email: Sender email address
        email_body: Readable plain-text body (fallback when there is no HTML)
        alias: Matched email alias (if any)
        email_html: Original email HTML (rendered in the note when present)

    Returns:
        True if successful, False otherwise
    """
    if not zd_ticket_id:
        return False

    try:
        if email_html:
            # Rendered note: the agent sees the email as sent (formatting, clickable
            # links, inline images). Sanitized here and again by Zendesk.
            result = post_zendesk_comment(
                zd_ticket_id=zd_ticket_id,
                is_internal=True,
                html_body=_build_email_note_html(parsed, subject, from_email, alias, email_html),
            )
        else:
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
            logger.info("Posted email + AI summary to Zendesk ticket %s", zd_ticket_id)
            return True
        else:
            logger.warning("Failed to post email + AI summary to Zendesk ticket %s", zd_ticket_id)
            return False

    except Exception as e:
        logger.error("Error posting to Zendesk for ticket %s: %s", zd_ticket_id, e)
        return False


def process_single_email(
    imap_conn: imaplib.IMAP4_SSL,
    uid: str,
    msg_data: bytes,
    ai_prompt: str,
) -> Optional[EmailLog]:
    """
    Process a single email message with alias-based matching and AI categorization.

    Args:
        imap_conn: IMAP connection
        uid: Email UID
        msg_data: Raw email data
        ai_prompt: AI analysis prompt template

    Returns:
        The created EmailLog or None if skipped

    NB: deliberately NOT wrapped in transaction.atomic. The only DB write is the
    EmailLog insert (atomic on its own); the surrounding work is external I/O —
    Zendesk reads, the LLM call, the Zendesk note, the IMAP flag. Holding a DB
    transaction open across those means long locks, and rolling the insert back
    because a *later* external side effect failed would discard the record of an
    email that was in fact processed (re-processing would then re-post to Zendesk).
    The Message-ID dedup + unique constraint guard against duplicates.
    """
    try:
        # Parse the email
        msg = email.message_from_bytes(msg_data)

        # Dedup: an email is processed at most once, ever. Read flags can't
        # guarantee that (unresolved mail is left UNSEEN on purpose), the
        # Message-ID can.
        message_id = (msg.get('Message-ID') or '').strip()[:512]
        if _is_duplicate_message(message_id):
            logger.info("Skipping already-processed email UID %s (Message-ID match)", uid)
            return None

        # Extract sender email
        from_email = extract_from_email(msg)
        if not from_email:
            logger.warning("Could not extract from_email from message UID %s", uid)
            return None

        # Step 1: Match email to a Zendesk ticket via recipient aliases (any domain).
        # No match → leave the email completely untouched (no AI, no log, no mark-read).
        ticket_data, alias = find_zendesk_ticket_for_email(msg)
        if not ticket_data:
            logger.info("No matching ticket — leaving email UID %s unread, not processed", uid)
            return None
        zd_ticket_id = str(ticket_data.get('id', ''))
        matched_via = 'alias'
        logger.info("✓ Matched alias %s to Zendesk ticket %s", alias, zd_ticket_id)

        # Try to find associated claim
        claim = Claim.objects.filter(zd_ticket_id=zd_ticket_id).first()

        # Backlog transition: the ticket exists in Zendesk but LORA has
        # not mirrored it yet. When enabled, import the real claim from
        # Zendesk on the spot so the matched email has somewhere to land.
        # We never fabricate a claim — this only copies one that already
        # exists in Zendesk (alias match guarantees a real ticket).
        if claim is None and getattr(
                SystemSettings.get_instance(), 'import_claims_from_email', False):
            from apps.integrations.services import import_claim_from_zendesk_ticket
            imported, created = import_claim_from_zendesk_ticket(zd_ticket_id)
            if imported is not None:
                claim = imported
                logger.info(
                    f"Imported claim #{claim.id} from Zendesk ticket "
                    f"{zd_ticket_id} on inbound email (created={created})")

        logger.info("Email will be posted to Zendesk ticket %s", zd_ticket_id)

        # Extract email body (readable text) + the original HTML (for the rendered
        # Zendesk note — same treatment as the manual per-ticket check).
        body = extract_email_body(msg)
        if not body:
            logger.warning("Empty body for message UID %s", uid)
            body = '(No content extracted)'
        email_html = extract_email_html(msg)

        # Get subject
        subject = decode_mime_header(msg.get('Subject', '(No Subject)'))

        # Extract raw headers for debugging
        raw_headers = extract_raw_headers(msg)

        # Extract to_email and delivered_to from headers (single source of truth
        # for the email regex lives in _first_email_in_header).
        to_email = _first_email_in_header(msg, 'To')
        delivered_to = _first_email_in_header(msg, 'Delivered-To')

        # Call Qwen AI for enhanced analysis (mask client PII when the claim is known)
        ai_result = call_qwen_ai(ai_prompt, body, subject, known_pii=_known_pii_for_email(claim))
        if ai_result.get('validation_failed'):
            # Schema validation failed inside call_qwen_ai; fall back to old parser
            # against the raw LLM output as a last-ditch effort
            parsed = parse_ai_response(ai_result.get('raw_response', ''))
        else:
            # New AIClient path returned structured fields directly
            parsed = {
                'summary': ai_result.get('summary', ''),
                'category': ai_result.get('category', EmailLog.CATEGORY_UNKNOWN),
                'action_required': ai_result.get('action_required', False),
                'auto_resolvable': ai_result.get('auto_resolvable', False),
            }

        # Determine if email should be auto-resolved
        auto_resolved = False
        should_mark_as_seen = False

        if parsed.get('auto_resolvable', False) and parsed.get('category') in AUTO_RESOLVABLE_CATEGORIES:
            auto_resolved = True
            should_mark_as_seen = True
            logger.info("Email auto-resolved: category=%s, UID=%s", parsed['category'], uid)
        else:
            # Leave unread for agent attention
            logger.info("Email requires agent attention: category=%s, UID=%s", parsed['category'], uid)

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
            logger.info("Posting email to Zendesk ticket %s...", zd_ticket_id)
            success = post_ai_summary_to_zendesk(
                zd_ticket_id=zd_ticket_id,
                parsed=parsed,
                subject=subject,
                from_email=from_email,
                email_body=body,
                alias=alias or '',
                email_html=email_html,
            )
            if success:
                logger.info("✓ Successfully posted email to Zendesk ticket %s", zd_ticket_id)
            else:
                logger.error("✗ Failed to post email to Zendesk ticket %s", zd_ticket_id)
        elif zd_ticket_id and matched_via != 'alias':
            logger.info(
                f"Skipping Zendesk posting for ticket {zd_ticket_id} — "
                f"matched via {matched_via}, not alias (risk of wrong ticket)"
            )
        else:
            logger.warning("Skipping Zendesk posting — no ticket ID or match")

        # Mark email as SEEN only if auto-resolved
        if should_mark_as_seen:
            mark_email_as_seen(imap_conn, uid)
        else:
            logger.debug("Leaving email UNSEEN for agent attention: UID=%s", uid)

        return email_log

    except IntegrityError:
        # A concurrent sweep inserted the same Message-ID first (unique constraint).
        # That's a clean dedup, not an error — skip rather than report a failure.
        logger.info("Email UID %s already inserted concurrently (Message-ID race) — skipping", uid)
        return None
    except Exception as e:
        logger.error("Error processing email UID %s: %s", uid, e, exc_info=True)
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
    except Exception as e:
        logger.error("Failed to load SystemSettings: %s", e)
        stats['errors'] += 1
        return stats

    # Validate credentials — reject blanks AND the decrypt-failure sentinel (a
    # truthy string), so a mis-keyed credential is never sent to the mail server
    # as a live password (repeated bad logins can lock the shared mailbox).
    if not all([imap_host, imap_user, imap_pass]) or any(
            is_decryption_failure(c) for c in (imap_host, imap_user, imap_pass)):
        logger.error("IMAP credentials not configured (or failed to decrypt)")
        stats['errors'] += 1
        return stats

    # Get configurable timeout
    imap_timeout = getattr(settings, 'IMAP_TIMEOUT', DEFAULT_IMAP_TIMEOUT)

    imap_conn = None
    try:
        # Step 1: Connect to IMAP
        logger.info("Connecting to IMAP server: %s", imap_host)
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

        # Newest-first so fresh case mail is always processed; ignored non-case
        # mail ages out of the lookback window on its own.
        uid_list = list(reversed(uid_list))[:MAX_EMAILS_PER_RUN]
        logger.info("Found %s UNSEEN emails (processing up to %s)", len(uid_list), MAX_EMAILS_PER_RUN)

        # Step 3: Process each email
        for uid in uid_list:
            stats['processed'] += 1
            uid_str = uid.decode('utf-8')

            try:
                # Fetch email by UID
                status, msg_data = imap_conn.fetch(uid, '(BODY.PEEK[])')

                if status != 'OK':
                    logger.warning("Failed to fetch email UID %s", uid_str)
                    stats['errors'] += 1
                    continue

                # Process the email
                result = process_single_email(
                    imap_conn,
                    uid_str,
                    msg_data[0][1],
                    ai_prompt,
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
                logger.error("Error processing email UID %s: %s", uid_str, e)
                stats['errors'] += 1
                continue

        logger.info("Email processing complete. Stats: %s", stats)

    except Exception as e:
        # Catch all exceptions including IMAP errors
        logger.error("IMAP error during email processing: %s", e)
        stats['errors'] += 1

    finally:
        # Clean up IMAP connection
        if imap_conn:
            try:
                imap_conn.close()
                imap_conn.logout()
                logger.info("IMAP connection closed")
            except Exception as e:
                logger.warning("Error closing IMAP connection: %s", e)

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
    creds = (system_settings.imap_host, system_settings.imap_user,
             system_settings.imap_pass)
    # Reject blanks AND the decrypt-failure sentinel (fail closed): never hand a
    # mis-decrypted credential to the mail server as a live password.
    if not all(creds) or any(is_decryption_failure(c) for c in creds):
        raise EmailNotConfigured('IMAP credentials not configured in System settings')
    conn = imaplib.IMAP4_SSL(system_settings.imap_host,
                             timeout=getattr(settings, 'IMAP_TIMEOUT', DEFAULT_IMAP_TIMEOUT))
    conn.login(system_settings.imap_user, system_settings.imap_pass)
    conn.select('INBOX')
    return conn


def search_alias_uids(conn: imaplib.IMAP4_SSL, alias: str) -> List[bytes]:
    """UIDs of unread mail from the window addressed to this alias.

    Checks both the To header and Delivered-To (alias delivery commonly
    surfaces only there); results are unioned.
    """
    # Self-guard: the alias is interpolated into the IMAP search term below, so
    # validate here too (callers should already have, but this can't be bypassed).
    _validate_alias(alias)
    since = imap_since_date()
    quoted = f'"{alias}"'
    uids = set()
    # To + Delivered-To catch the common cases; X-AnonAddy-Original-To is the
    # cleanest signal of which alias an AnonAddy-forwarded message was really
    # for (the visible To can be the encoded alias+contact form).
    for criteria in (('TO', quoted),
                     ('HEADER', 'Delivered-To', quoted),
                     ('HEADER', 'X-AnonAddy-Original-To', quoted)):
        status, messages = conn.search(None, 'UNSEEN', 'SINCE', since, *criteria)
        if status == 'OK' and messages and messages[0]:
            uids.update(messages[0].split())
    return sorted(uids, key=int)


def _ai_tags_for(category: str, action_required: bool) -> set[str]:
    tags: set[str] = set()
    if category in AI_TAG_BY_CATEGORY:
        tags.add(AI_TAG_BY_CATEGORY[category])
    if action_required:
        tags.add(AI_TAG_ATTENTION)
    return tags


def _first_email_in_header(msg: email.message.Message, header_name: str) -> str:
    """The first email address found in a single header, lower-cased ('' if none).
    The one place a single address is pulled from a header — every caller routes
    through here so the extraction regex lives in exactly one spot."""
    value = decode_mime_header(msg.get(header_name, '') or '')
    match = _PLAIN_EMAIL_RE.search(value)
    return match.group(0).lower() if match else ''


def _process_ticket_email(
    conn: imaplib.IMAP4_SSL,
    uid: str,
    msg: email.message.Message,
    message_id: str,
    ticket_id: str,
    claim: Optional[Claim],
    alias: str,
    ai_prompt: str,
) -> Dict[str, Any]:
    """Process one email already known to belong to ticket_id: AI
    categorization, EmailLog, internal note. Returns a UI-friendly entry."""
    from_email = extract_from_email(msg) or ''
    subject = decode_mime_header(msg.get('Subject', '(No Subject)'))
    body = extract_email_body(msg) or '(No content extracted)'
    email_html = extract_email_html(msg)

    ai_result = call_qwen_ai(ai_prompt, body, subject, known_pii=_known_pii_for_email(claim))
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
        category=parsed.get('category', EmailLog.CATEGORY_UNKNOWN),
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
        email_html=email_html,
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
        'category': parsed.get('category', EmailLog.CATEGORY_UNKNOWN),
        'summary': parsed.get('summary', ''),
        'action_required': bool(parsed.get('action_required')),
        'auto_resolved': auto_resolved,
        'note_posted': note_posted,
    }


def check_email_for_ticket(ticket_id: str, claim: Optional[Claim], alias: str) -> Dict[str, Any]:
    """Check the mailbox for new mail addressed to one ticket's alias.

    Each new email gets: AI categorization, an EmailLog row (linked to the
    claim when there is one), an internal note on the ticket, and — per
    category — additive ai_* tags on the ticket.

    Raises EmailNotConfigured when IMAP credentials are missing. IMAP
    connection errors propagate; per-email failures are counted, not raised.
    """
    _validate_alias(alias)

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
                status, msg_data = conn.fetch(uid, '(BODY.PEEK[])')
                if status != 'OK' or not msg_data or msg_data[0] is None:
                    logger.warning("Email check: failed to fetch UID %s", uid_str)
                    results['errors'] += 1
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                message_id = (msg.get('Message-ID') or '').strip()[:512]
                if _is_duplicate_message(message_id):
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

    # A new email is real new information about the case → refresh the one
    # stored summary so the LORA app and the Zendesk sidebar both reflect it.
    # Best-effort: never let a summary hiccup affect the email result.
    if claim and results['processed']:
        try:
            from apps.integrations.services import (
                fetch_zendesk_ticket, fetch_zendesk_comments)
            from apps.integrations.briefing import refresh_claim_summary
            ticket_data = fetch_zendesk_ticket(str(ticket_id)) or {}
            ticket_data['comments'] = fetch_zendesk_comments(str(ticket_id))
            refresh_claim_summary(claim, ticket_data)
        except Exception as e:
            logger.warning(
                f"Email check: summary refresh failed for claim #{claim.id}: {e}")

    return results
