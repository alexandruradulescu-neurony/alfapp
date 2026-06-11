"""
Zendesk integration services for LORA.
Handles posting comments and fetching ticket data.
"""

import base64
import json
import logging
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import List, Dict, Any, Optional

from django.conf import settings
from django.core.cache import cache

from apps.config.models import SystemSettings

logger = logging.getLogger(__name__)


def safe_date(value):
    """Parse a Zendesk date string ('YYYY-MM-DD') into a date, or None on failure."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def safe_decimal(value):
    """Parse a numeric value into Decimal, or None on failure."""
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None

# ---------------------------------------------------------------------------
# Zendesk custom field IDs (populated by the marketing site for every new
# ticket).  Confirmed against the live Zendesk field list on 2026-06-10.
# When a field ID is None, the extractor falls back to LLM extraction for
# that field.
# ---------------------------------------------------------------------------
ZENDESK_FIELD_ALIAS_EMAIL: int = 13606076120860   # "Email used for submissions" (our per-case alias)
ZENDESK_FIELD_CLIENT_EMAIL: int = 13737499349020  # "Customer Email" (the client's real email)
ZENDESK_FIELD_CLIENT_NAME: int = 13737514170140   # "Customer Name"
ZENDESK_FIELD_PHONE: int = 11761070082844         # "Phone Number"
ZENDESK_FIELD_CLAIM_NUMBER: int = 11688794648732  # "Claim #"

# Flight info is spread across several fields; flight_details is composed from all.
ZENDESK_FIELD_FLIGHT: int = 13737630819996        # "Flight Number"
ZENDESK_FIELD_AIRLINE: int = 11761080032028       # "Airline"
ZENDESK_FIELD_AIRPORT: int = 11761104069276       # "Airport"
ZENDESK_FIELD_SEAT: int = 13737646294940          # "Seat Number"
ZENDESK_FIELD_DATETIME: int = 13737598795292      # "Date & Time"

# The lost item is described across two fields; object_description is composed
# from both. "Lost Object" holds the item itself; "Object Details" holds extra detail.
ZENDESK_FIELD_LOST_OBJECT: int = 11761123532444   # "Lost Object"
ZENDESK_FIELD_OBJECT_DETAILS: int = 13737436477852  # "Object Details"

# Extended fields wired 2026-06-10 (see docs/ZENDESK_FIELDS.md for the full map).
ZENDESK_FIELD_BILLING_ADDRESS: int = 13737449416988   # "Billing Address"
ZENDESK_FIELD_SHIPPING_ADDRESS: int = 11949784750236  # "Shipping Address"
ZENDESK_FIELD_INCIDENT_DETAILS: int = 13737603591964  # "Incident Details"
ZENDESK_FIELD_LOST_LOCATION: int = 16314445118492     # "Lost Location"
ZENDESK_FIELD_DEADLINE_DATE: int = 14394267216668     # "Deadline Date"
ZENDESK_FIELD_DEADLINE_TIME: int = 14394267218972     # "Deadline Time"
ZENDESK_FIELD_DEADLINE_TZ: int = 14394267222684       # "Deadline Time Zone"
ZENDESK_FIELD_PRICE_PAID: int = 19736734259996        # "Price Paid" (numeric)
ZENDESK_FIELD_PAYMENT_METHOD: int = 14495509913244    # "Payment Method"
ZENDESK_FIELD_PAYMENT_STATUS: int = 11761180893980    # "Payment Status"
ZENDESK_FIELD_WOOCOMMERCE_ID: int = 13484164181916    # "WooCommerce ID"
ZENDESK_FIELD_TRACKING_INFO: int = 11949753094556     # "3rd Party Tracking Information"


def _get_zendesk_auth_headers() -> Dict[str, str]:
    """
    Generate Basic Auth headers for Zendesk API.
    Uses email/token authentication.
    """
    system_settings = SystemSettings.get_instance()
    
    email = system_settings.zd_email
    token = system_settings.zd_token
    subdomain = system_settings.zd_subdomain
    
    if not all([email, token, subdomain]):
        raise ValueError("Zendesk credentials not configured in SystemSettings")
    
    # Zendesk token auth: email/token
    credentials = f"{email}/token:{token}"
    encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    
    return {
        'Authorization': f'Basic {encoded_credentials}',
        'Content-Type': 'application/json',
    }


def _get_zendesk_base_url() -> str:
    """
    Get the base URL for Zendesk API.
    """
    system_settings = SystemSettings.get_instance()
    subdomain = system_settings.zd_subdomain
    
    if not subdomain:
        raise ValueError("Zendesk subdomain not configured in SystemSettings")
    
    return f"https://{subdomain}.zendesk.com/api/v2"


def post_zendesk_comment(zd_ticket_id: str, comment_body: str, is_internal: bool = True) -> Optional[Dict[str, Any]]:
    """
    Post a comment to a Zendesk ticket.
    
    Args:
        zd_ticket_id: The Zendesk ticket ID
        comment_body: The comment text to post
        is_internal: If True, post as internal note (default True)
    
    Returns:
        The response data dict on success, None on failure
    
    Raises:
        ValueError: If Zendesk credentials not configured
    """
    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()

        # Zendesk API v2: comments are added via ticket update (PUT), not a separate endpoint
        url = f"{base_url}/tickets/{zd_ticket_id}.json"

        # Build the request payload
        payload = {
            'ticket': {
                'comment': {
                    'body': comment_body,
                    'public': not is_internal,  # Internal note if not public
                }
            }
        }

        data = json.dumps(payload).encode('utf-8')

        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method='PUT'
        )

        logger.info(f"Posting comment to Zendesk ticket {zd_ticket_id}")

        # Use configurable timeout
        timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
            logger.info(f"Successfully posted comment to ticket {zd_ticket_id}")
            return result
            
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error posting to Zendesk ticket {zd_ticket_id}: {e.code} - {error_body}")
        return None
        
    except urllib.error.URLError as e:
        logger.error(f"URL error posting to Zendesk ticket {zd_ticket_id}: {e.reason}")
        return None
        
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return None
        
    except Exception as e:
        logger.error(f"Unexpected error posting to Zendesk ticket {zd_ticket_id}: {e}")
        return None


def fetch_zendesk_comments(zd_ticket_id: str) -> List[Dict[str, Any]]:
    """
    Fetch all comments for a Zendesk ticket.
    
    Args:
        zd_ticket_id: The Zendesk ticket ID
    
    Returns:
        List of dicts with keys: author, body, created_at
        Returns empty list on failure
    """
    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()
        
        url = f"{base_url}/tickets/{zd_ticket_id}/comments.json"
        
        req = urllib.request.Request(
            url,
            headers=headers,
            method='GET'
        )
        
        logger.info(f"Fetching comments from Zendesk ticket {zd_ticket_id}")
        
        # Use configurable timeout
        timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
            comments_data = result.get('comments', [])
            
            # Transform to simplified format
            comments = []
            for comment in comments_data:
                author = comment.get('author', {})
                comments.append({
                    'id': comment.get('id'),
                    'author': {
                        'id': author.get('id'),
                        'name': author.get('name', 'Unknown'),
                        'email': author.get('email', ''),
                    },
                    'body': comment.get('body', ''),
                    'public': comment.get('public', False),
                    'created_at': comment.get('created_at'),
                })
            
            logger.info(f"Fetched {len(comments)} comments from ticket {zd_ticket_id}")
            return comments
            
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error fetching comments from Zendesk ticket {zd_ticket_id}: {e.code} - {error_body}")
        return []
        
    except urllib.error.URLError as e:
        logger.error(f"URL error fetching comments from Zendesk ticket {zd_ticket_id}: {e.reason}")
        return []
        
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        return []
        
    except Exception as e:
        logger.error(f"Unexpected error fetching comments from Zendesk ticket {zd_ticket_id}: {e}")
        return []


def fetch_zendesk_ticket(zd_ticket_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a single Zendesk ticket by ID.
    
    Args:
        zd_ticket_id: The Zendesk ticket ID
    
    Returns:
        Ticket data dict on success, None on failure
    """
    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()
        
        url = f"{base_url}/tickets/{zd_ticket_id}.json"
        
        req = urllib.request.Request(
            url,
            headers=headers,
            method='GET'
        )
        
        # Use configurable timeout
        timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
            ticket = result.get('ticket', {})
            
            logger.info(f"Fetched Zendesk ticket {zd_ticket_id}")
            return {
                'id': ticket.get('id'),
                'subject': ticket.get('subject'),
                'description': ticket.get('description'),  # Full ticket description
                'status': ticket.get('status'),
                'priority': ticket.get('priority'),
                'requester_id': ticket.get('requester_id'),
                'assignee_id': ticket.get('assignee_id'),
                'created_at': ticket.get('created_at'),
                'updated_at': ticket.get('updated_at'),
                'type': ticket.get('type'),
                'due_at': ticket.get('due_at'),
                'tags': ticket.get('tags'),
                'custom_fields': ticket.get('custom_fields'),
            }
            
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error fetching Zendesk ticket {zd_ticket_id}: {e.code} - {error_body}")
        return None
        
    except urllib.error.URLError as e:
        logger.error(f"URL error fetching Zendesk ticket {zd_ticket_id}: {e.reason}")
        return None
        
    except Exception as e:
        logger.error(f"Unexpected error fetching Zendesk ticket {zd_ticket_id}: {e}")
        return None


def fetch_zendesk_user(user_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch user details from Zendesk API.

    Args:
        user_id: Zendesk user ID

    Returns:
        User data dict with email, name, etc. on success, None on failure
    """
    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()

        url = f"{base_url}/users/{user_id}.json"

        req = urllib.request.Request(
            url,
            headers=headers,
            method='GET'
        )

        timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
            user = result.get('user', {})

            logger.info(f"Fetched Zendesk user {user_id}: {user.get('email', 'no email')}")
            return {
                'id': user.get('id'),
                'email': user.get('email'),
                'name': user.get('name'),
                'phone': user.get('phone'),
            }

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error fetching Zendesk user {user_id}: {e.code} - {error_body}")
        return None

    except urllib.error.URLError as e:
        logger.error(f"URL error fetching Zendesk user {user_id}: {e.reason}")
        return None

    except Exception as e:
        logger.error(f"Unexpected error fetching Zendesk user {user_id}: {e}")
        return None


CUSTOM_STATUS_CACHE_KEY = 'zd_custom_statuses_v1'
CUSTOM_STATUS_CACHE_TTL = 60 * 60 * 24  # 24h; unknown ids force a refresh anyway


def _fetch_custom_statuses() -> Dict[str, Dict[str, str]]:
    """GET /api/v2/custom_statuses.json -> {id: {'name', 'category'}}.
    Raises on configuration/network errors (caller decides the fallback)."""
    base_url = _get_zendesk_base_url()
    headers = _get_zendesk_auth_headers()
    req = urllib.request.Request(f"{base_url}/custom_statuses.json", headers=headers, method='GET')
    timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = json.loads(response.read().decode('utf-8'))
    mapping = {}
    for cs in result.get('custom_statuses', []):
        mapping[str(cs.get('id'))] = {
            'name': cs.get('agent_label', '') or '',
            'category': cs.get('status_category', '') or '',
        }
    logger.info(f"Fetched {len(mapping)} Zendesk custom statuses")
    return mapping


def resolve_custom_status(status_id) -> Dict[str, str]:
    """Translate a Zendesk custom-status id to {'name', 'category'}.
    Cached; an unknown id forces one refresh (covers statuses added in
    Zendesk after the cache was filled). Total failure -> id as name,
    empty category — the webhook still mirrors *something* traceable."""
    sid = str(status_id)
    mapping = cache.get(CUSTOM_STATUS_CACHE_KEY)
    if mapping is None or sid not in mapping:
        try:
            mapping = _fetch_custom_statuses()
            cache.set(CUSTOM_STATUS_CACHE_KEY, mapping, CUSTOM_STATUS_CACHE_TTL)
        except Exception as e:
            logger.error(f"Could not fetch Zendesk custom statuses: {e}")
            mapping = mapping or {}
    entry = mapping.get(sid)
    if not entry:
        logger.warning(f"Unknown Zendesk custom status id {sid}; mirroring id verbatim")
        return {'name': sid, 'category': ''}
    return entry


def create_zendesk_ticket(
    subject: str,
    comment_body: str,
    requester_email: str,
    tags: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """
    Create a new Zendesk ticket.
    
    Args:
        subject: Ticket subject
        comment_body: Initial comment body
        requester_email: Email of the requester
        tags: Optional list of tags
    
    Returns:
        Ticket data dict on success, None on failure
    """
    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()
        
        url = f"{base_url}/tickets.json"
        
        payload = {
            'ticket': {
                'subject': subject,
                'comment': {
                    'body': comment_body,
                },
                'requester': {
                    'email': requester_email,
                },
                'tags': tags or ['lora', 'lost-object'],
            }
        }
        
        data = json.dumps(payload).encode('utf-8')
        
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method='POST'
        )
        
        logger.info(f"Creating Zendesk ticket for {requester_email}")
        
        # Use configurable timeout
        timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
            ticket = result.get('ticket', {})
            
            logger.info(f"Created Zendesk ticket #{ticket.get('id')} for {requester_email}")
            return {
                'id': ticket.get('id'),
                'subject': ticket.get('subject'),
                'status': ticket.get('status'),
                'url': ticket.get('url'),
            }
            
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error creating Zendesk ticket: {e.code} - {error_body}")
        return None
        
    except urllib.error.URLError as e:
        logger.error(f"URL error creating Zendesk ticket: {e.reason}")
        return None
        
    except Exception as e:
        logger.error(f"Unexpected error creating Zendesk ticket: {e}")
        return None



def search_zendesk_tickets(query: str) -> List[Dict[str, Any]]:
    """
    Search Zendesk tickets using the Search API.

    Args:
        query: Search query string (Zendesk search syntax)

    Returns:
        List of ticket dicts, empty list on failure

    Security Note:
        Query parameter is safely URL-encoded using urllib.parse.urlencode
        to prevent injection attacks.
    """
    # Validate input to prevent empty or excessively long queries
    if not query or not query.strip():
        logger.warning("Empty search query provided to search_zendesk_tickets")
        return []

    if len(query) > 1000:
        logger.warning(f"Search query too long ({len(query)} chars), truncating to 1000")
        query = query[:1000]

    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()

        # Zendesk Search API endpoint
        url = f"{base_url}/search.json"

        # Build query params with proper URL encoding to prevent injection
        params = urllib.parse.urlencode({'query': query, 'type': 'ticket'})
        full_url = f"{url}?{params}"

        req = urllib.request.Request(
            full_url,
            headers=headers,
            method='GET'
        )

        logger.info(f"Searching Zendesk tickets: {query[:100]}...")

        # Use configurable timeout
        timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
            results = result.get('results', [])

            logger.info(f"Found {len(results)} tickets matching: {query[:100]}...")
            return results

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error searching Zendesk tickets: {e.code} - {error_body}")
        return []

    except urllib.error.URLError as e:
        logger.error(f"URL error searching Zendesk tickets: {e.reason}")
        return []

    except Exception as e:
        logger.error(f"Unexpected error searching Zendesk tickets: {e}")
        return []


def fetch_zendesk_ticket_full(ticket_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a complete Zendesk ticket including custom fields.
    
    Args:
        ticket_id: The Zendesk ticket ID
    
    Returns:
        Full ticket data dict with custom fields, None on failure
    """
    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()
        
        # Fetch ticket with custom fields included
        url = f"{base_url}/tickets/{ticket_id}.json"
        
        req = urllib.request.Request(
            url,
            headers=headers,
            method='GET'
        )
        
        logger.info(f"Fetching full Zendesk ticket {ticket_id}")
        
        # Use configurable timeout
        timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
            ticket = result.get('ticket', {})
            
            if ticket:
                logger.info(f"Fetched full Zendesk ticket {ticket_id}")
                return {
                    'id': ticket.get('id'),
                    'subject': ticket.get('subject'),
                    'description': ticket.get('description'),
                    'status': ticket.get('status'),
                    'priority': ticket.get('priority'),
                    'requester_id': ticket.get('requester_id'),
                    'assignee_id': ticket.get('assignee_id'),
                    'custom_fields': ticket.get('custom_fields', []),
                    'tags': ticket.get('tags', []),
                    'created_at': ticket.get('created_at'),
                    'updated_at': ticket.get('updated_at'),
                    'url': ticket.get('url'),
                }
            return None
            
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error fetching Zendesk ticket {ticket_id}: {e.code} - {error_body}")
        return None
        
    except urllib.error.URLError as e:
        logger.error(f"URL error fetching Zendesk ticket {ticket_id}: {e.reason}")
        return None
        
    except Exception as e:
        logger.error(f"Unexpected error fetching Zendesk ticket {ticket_id}: {e}")
        return None


def search_zendesk_ticket_for_dispute(
    buyer_email: str,
    buyer_name: str = '',
    transaction_id: str = '',
    transaction_date: str = ''
) -> Optional[Dict[str, Any]]:
    """
    Search for a Zendesk ticket matching a PayPal dispute.
    Uses multi-strategy search to find the related ticket.
    
    Args:
        buyer_email: Buyer's email address
        buyer_name: Buyer's name (optional)
        transaction_id: PayPal transaction ID (optional)
        transaction_date: Transaction date (optional)
    
    Returns:
        First matching ticket data dict, None if no match
    """
    def _pick_best_result(results: list, transaction_date: str = '') -> Optional[Dict[str, Any]]:
        """Pick the most recent ticket from search results."""
        if not results:
            return None
        # Sort by created_at descending to get the most recent ticket
        sorted_results = sorted(
            results,
            key=lambda t: t.get('created_at', ''),
            reverse=True,
        )
        return sorted_results[0]

    # Strategy 1: Search by buyer email
    if buyer_email:
        query = f'requester:{buyer_email}'
        results = search_zendesk_tickets(query)
        best = _pick_best_result(results, transaction_date)
        if best:
            logger.info(f"Found ticket by email search: {best.get('id')}")
            return best

    # Strategy 2: Search by transaction ID in ticket description/comments
    if transaction_id:
        query = f'{transaction_id} in description'
        results = search_zendesk_tickets(query)
        best = _pick_best_result(results)
        if best:
            logger.info(f"Found ticket by transaction ID search: {best.get('id')}")
            return best

    # Strategy 3: Search by buyer name + date
    if buyer_name and transaction_date:
        query = f'"{buyer_name}" created>{transaction_date}'
        results = search_zendesk_tickets(query)
        best = _pick_best_result(results, transaction_date)
        if best:
            logger.info(f"Found ticket by name+date search: {best.get('id')}")
            return best

    # Strategy 4: Search by buyer name only
    if buyer_name:
        query = f'"{buyer_name}"'
        results = search_zendesk_tickets(query)
        best = _pick_best_result(results)
        if best:
            logger.info(f"Found ticket by name search: {best.get('id')}")
            return best
    
    logger.info(f"No matching Zendesk ticket found for dispute (email: {buyer_email})")
    return None


# Zendesk custom field holding the per-ticket inbound email alias
# (e.g. "client-123@mydomain.com"). See docs/ZENDESK_FIELDS.md.
EMAIL_ALIAS_FIELD_ID = '13606076120860'


def get_ticket_email_alias(ticket_data: Dict[str, Any]) -> str:
    """Read the email alias custom field from a fetched Zendesk ticket payload.

    Returns the alias lowercased, or '' when the field is absent/empty.
    """
    for field in ticket_data.get('custom_fields') or []:
        if str(field.get('id')) == EMAIL_ALIAS_FIELD_ID:
            value = (field.get('value') or '').strip().lower()
            return value
    return ''


def add_zendesk_ticket_tags(zd_ticket_id: str, tags: List[str]) -> bool:
    """Add tags to a Zendesk ticket WITHOUT touching its existing tags.

    Uses the dedicated tags endpoint with PUT, which Zendesk defines as
    additive (POST on the same endpoint would REPLACE the whole tag set —
    never use it here). Does not work on closed tickets; failure is logged
    and reported, never raised.
    """
    if not tags:
        return True
    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()
        url = f"{base_url}/tickets/{zd_ticket_id}/tags.json"
        req = urllib.request.Request(
            url,
            data=json.dumps({'tags': tags}).encode('utf-8'),
            headers=headers,
            method='PUT',
        )
        with urllib.request.urlopen(req, timeout=30):
            logger.info(f"Added tags {tags} to Zendesk ticket {zd_ticket_id}")
            return True
    except Exception as e:
        logger.error(f"Error adding tags {tags} to Zendesk ticket {zd_ticket_id}: {e}")
        return False


def match_alias_to_zendesk_ticket(alias: str) -> Optional[Dict[str, Any]]:
    """
    Search for a Zendesk ticket where custom field 13606076120860 contains the email alias.

    This is the ONLY matching method - no fallback to other fields.

    Args:
        alias: The email alias to search for (e.g., "client-123@mydomain.com")

    Returns:
        Matching ticket data dict, None if no match
    """
    try:
        # Search for tickets where the custom field contains the alias
        # Zendesk search syntax: custom_fields_{id}:"value"
        query = f'custom_fields_{EMAIL_ALIAS_FIELD_ID}:"{alias}"'
        results = search_zendesk_tickets(query)
        
        if results:
            logger.info(f"Matched alias {alias} to Zendesk ticket {results[0].get('id')}")
            return results[0]
        
        logger.debug(f"No Zendesk ticket found for alias {alias}")
        return None
        
    except Exception as e:
        logger.error(f"Error matching alias to Zendesk ticket: {e}")
        return None


def tag_zendesk_ticket_as_refunded(zd_ticket_id: str) -> bool:
    """
    Add 'refunded' tag to a Zendesk ticket.
    
    This mimics the existing PHP logic for the "normal route" where
    WordPress processes a refund and tags the Zendesk ticket.
    
    Args:
        zd_ticket_id: The Zendesk ticket ID to tag
    
    Returns:
        True if successful, False otherwise
    """
    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()
        
        url = f"{base_url}/tickets/{zd_ticket_id}.json"
        
        # Add 'refunded' tag to existing tags
        payload = {
            'ticket': {
                'tags': ['refunded']  # This appends to existing tags
            }
        }
        
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method='PUT'
        )
        
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            logger.info(f"Added 'refunded' tag to Zendesk ticket {zd_ticket_id}")
            return True
            
    except Exception as e:
        logger.error(f"Error tagging Zendesk ticket as refunded: {e}")
        return False


def add_refund_comment_to_zendesk(
    zd_ticket_id: str,
    refund_amount: str,
    refund_id: str,
    reason: str,
    is_internal: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    Add a comment to Zendesk ticket about a refund.
    
    Args:
        zd_ticket_id: The Zendesk ticket ID
        refund_amount: Refund amount with currency
        refund_id: Refund transaction ID
        reason: Reason for the refund
        is_internal: If True, post as internal note
    
    Returns:
        Response data dict on success, None on failure
    """
    try:
        comment = (
            f"💰 **Refund Processed**\n\n"
            f"- **Amount**: {refund_amount}\n"
            f"- **Refund ID**: {refund_id}\n"
            f"- **Reason**: {reason}\n\n"
            f"Refund has been processed via PayPal."
        )
        
        return post_zendesk_comment(zd_ticket_id, comment, is_internal=is_internal)
        
    except Exception as e:
        logger.error(f"Error adding refund comment to Zendesk: {e}")
        return None


def _get_custom_field_value(custom_fields: list, field_id: int | None) -> str:
    """Return the string value of a Zendesk custom field, or '' if absent/None."""
    if field_id is None:
        return ''
    if not isinstance(custom_fields, (list, tuple)):
        return ''
    for field in custom_fields:
        if isinstance(field, dict) and field.get('id') == field_id:
            value = field.get('value')
            return str(value) if value else ''
    return ''


def _compose_flight_details(custom_fields: list) -> str:
    """Compose a single labeled flight_details string from the separate Zendesk
    flight fields (number, airline, airport, seat, date/time). Only present
    fields are included, joined with ' | '. Returns '' if none are set."""
    segments = [
        ("Flight", _get_custom_field_value(custom_fields, ZENDESK_FIELD_FLIGHT)),
        ("Airline", _get_custom_field_value(custom_fields, ZENDESK_FIELD_AIRLINE)),
        ("Airport", _get_custom_field_value(custom_fields, ZENDESK_FIELD_AIRPORT)),
        ("Seat", _get_custom_field_value(custom_fields, ZENDESK_FIELD_SEAT)),
        ("Date/Time", _get_custom_field_value(custom_fields, ZENDESK_FIELD_DATETIME)),
    ]
    return " | ".join(f"{label}: {value}" for label, value in segments if value)


def _compose_object_description(custom_fields: list) -> str:
    """Compose object_description from 'Lost Object' (the item) and 'Object
    Details' (extra detail). Item first, then details on the next line. Returns
    '' if neither is set (caller falls back to the LLM-extracted value)."""
    parts = [
        _get_custom_field_value(custom_fields, ZENDESK_FIELD_LOST_OBJECT),
        _get_custom_field_value(custom_fields, ZENDESK_FIELD_OBJECT_DETAILS),
    ]
    return "\n".join(p for p in parts if p)


def analyze_zendesk_ticket_for_claim(ticket_data: Dict[str, Any]) -> Dict[str, str]:
    """Extract claim information from a Zendesk ticket payload.

    Strategy (structured-fields-first):
    1. Read structured Zendesk custom fields directly from the ticket payload
       for any field whose ID is confirmed (non-None constant above).
    2. Pass ONLY the free-text description to the LLM; the LLM fills in
       object_description and additional_context.
    3. Merge the two sources — structured fields win over LLM for the fields
       they cover; LLM handles what structured fields cannot.

    Returns a dict with keys:
        client_email, flight_details, object_description, phone,
        alternate_email
    (Empty strings for fields not found — shape is identical to the old
    implementation so all downstream callers are unaffected.)
    """
    from apps.communications.services import call_qwen_ai_for_ticket_extraction

    try:
        subject = ticket_data.get('subject', '')
        description = ticket_data.get('description', '')
        comments = ticket_data.get('comments', [])
        custom_fields = ticket_data.get('custom_fields') or []

        # ------------------------------------------------------------------
        # Step 1: Read structured custom fields
        # ------------------------------------------------------------------
        alias_email = _get_custom_field_value(custom_fields, ZENDESK_FIELD_ALIAS_EMAIL)
        client_email_structured = _get_custom_field_value(custom_fields, ZENDESK_FIELD_CLIENT_EMAIL)
        client_name_structured = _get_custom_field_value(custom_fields, ZENDESK_FIELD_CLIENT_NAME)
        phone_structured = _get_custom_field_value(custom_fields, ZENDESK_FIELD_PHONE)
        claim_number_structured = _get_custom_field_value(custom_fields, ZENDESK_FIELD_CLAIM_NUMBER)
        flight_composed = _compose_flight_details(custom_fields)
        object_composed = _compose_object_description(custom_fields)

        # Extended structured fields (raw string values; the view coerces
        # deadline_date and price_paid to their DB types defensively).
        extended = {
            'billing_address': _get_custom_field_value(custom_fields, ZENDESK_FIELD_BILLING_ADDRESS),
            'shipping_address': _get_custom_field_value(custom_fields, ZENDESK_FIELD_SHIPPING_ADDRESS),
            'incident_details': _get_custom_field_value(custom_fields, ZENDESK_FIELD_INCIDENT_DETAILS),
            'lost_location': _get_custom_field_value(custom_fields, ZENDESK_FIELD_LOST_LOCATION),
            'deadline_date': _get_custom_field_value(custom_fields, ZENDESK_FIELD_DEADLINE_DATE),
            'deadline_time': _get_custom_field_value(custom_fields, ZENDESK_FIELD_DEADLINE_TIME),
            'deadline_timezone': _get_custom_field_value(custom_fields, ZENDESK_FIELD_DEADLINE_TZ),
            'price_paid': _get_custom_field_value(custom_fields, ZENDESK_FIELD_PRICE_PAID),
            'payment_method': _get_custom_field_value(custom_fields, ZENDESK_FIELD_PAYMENT_METHOD),
            'payment_status': _get_custom_field_value(custom_fields, ZENDESK_FIELD_PAYMENT_STATUS),
            'woocommerce_id': _get_custom_field_value(custom_fields, ZENDESK_FIELD_WOOCOMMERCE_ID),
            'tracking_info': _get_custom_field_value(custom_fields, ZENDESK_FIELD_TRACKING_INFO),
        }

        # The alias is used as known_pii so the tokenizer tags it as ALIAS
        # instead of EMAIL — preventing the LLM from treating it as the
        # client's real address.
        known_aliases = [alias_email] if alias_email else []

        # ------------------------------------------------------------------
        # Step 2: Build free-text context and call LLM for unstructured fields
        # ------------------------------------------------------------------
        context = f"Ticket Subject: {subject}\n\n"
        context += f"Ticket Description:\n{description}\n\n"

        if comments:
            context += "Comments:\n"
            for comment in comments[:5]:  # Limit to first 5 comments
                author = comment.get('author', {}).get('name', 'Unknown')
                body = comment.get('body', '')
                context += f"{author}: {body}\n\n"

        prompt = (
            "Extract the following information from this Zendesk ticket about a lost object claim. "
            "The customer's name, email, phone, and flight details may already be available in "
            "structured form — focus on the free-text description of the lost item and any "
            "additional context that would help locate it.\n\n"
            "Return a JSON object with:\n"
            '  "object_description": "detailed description of the lost item",\n'
            '  "additional_context": "any extra context about the loss event (optional)"\n\n'
            "Return null for fields not found.\n\n"
            "Ticket Content:\n"
        )

        llm_result = call_qwen_ai_for_ticket_extraction(
            prompt=prompt,
            ticket_context=context,
            known_aliases=known_aliases,
        )

        logger.debug(
            "LLM extraction result for ticket %s: %r",
            ticket_data.get('id', 'unknown'),
            llm_result,
        )

        # ------------------------------------------------------------------
        # Step 3: Merge — structured fields take precedence where confirmed
        # ------------------------------------------------------------------
        extracted = {
            # email: structured field wins; fall back to LLM is not done here
            # because TicketExtraction schema does not extract email — the
            # caller (views.py) resolves email via requester_id fallback.
            'client_email': client_email_structured,
            'client_name': client_name_structured,
            # flight: composed from the structured flight fields.
            'flight_details': flight_composed,
            # object: structured composition wins; fall back to the LLM-extracted
            # description only when neither structured object field is populated.
            'object_description': object_composed or llm_result.get('object_description', ''),
            'phone': phone_structured,
            'alternate_email': '',
            # claim_number: the view uses this (with subject-line fallback) to
            # resolve the ALF claim ID.
            'claim_number': claim_number_structured,
            **extended,
        }

        logger.info(
            "Extraction completed for ticket %s (structured email=%r, name=%r, "
            "flight=%r, phone=%r, object=%r, claim_no=%r; LLM object fallback used=%r)",
            ticket_data.get('id', 'unknown'),
            bool(client_email_structured),
            bool(client_name_structured),
            bool(flight_composed),
            bool(phone_structured),
            bool(object_composed),
            bool(claim_number_structured),
            bool(not object_composed and llm_result.get('object_description', '')),
        )
        return extracted

    except Exception as e:
        logger.error(f"Error in extraction for Zendesk ticket: {e}", exc_info=True)
        return {
            'client_email': '',
            'client_name': '',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
            'claim_number': '',
            'billing_address': '',
            'shipping_address': '',
            'incident_details': '',
            'lost_location': '',
            'deadline_date': '',
            'deadline_time': '',
            'deadline_timezone': '',
            'price_paid': '',
            'payment_method': '',
            'payment_status': '',
            'woocommerce_id': '',
            'tracking_info': '',
        }


CLIENT_UPDATE_CADENCE_DAYS = (2, 5, 11, 20)


def build_claim_facts(claim) -> dict:
    """Compact, panel-ready facts for the Zendesk sidebar Briefing tab.
    Uses only LORA-side data the Zendesk ticket does not already have.

    Keys:
    - 'status': verbatim Zendesk status name (sidebar renders it as-is; do not rename).
    - 'status_family': claim.status_category ('new'/'open'/'pending'/'hold'/'solved').
    - 'deadline': ISO date string for display.  The human-entered deadline_date wins
      when present (exact date as entered); deadline_at (computed moment) is used
      only as a fallback when deadline_date is absent.  deadline_at is for urgency
      math only; displaying it avoids the one-day-late risk from timezone conversion.
    - 'disputes_total': count; no dependence on the Dispute status enum.
    - 'next_update_due': next client-update milestone (day 2/5/11/20 after claim
      creation) that hasn't passed yet; None when all milestones are past OR when
      status_category is 'solved' (cadence is suppressed for closed claims)."""
    from datetime import timedelta
    from django.utils import timezone
    from apps.payments.models import Dispute

    emails = claim.emails.all()

    next_update_due = None
    if claim.status_category != 'solved':
        base = timezone.localtime(claim.created_at).date()
        today = timezone.localdate()
        for day in CLIENT_UPDATE_CADENCE_DAYS:
            due = base + timedelta(days=day)
            if due >= today:
                next_update_due = {'day': day, 'date': due.isoformat()}
                break

    deadline = None
    if claim.deadline_date:
        deadline = claim.deadline_date.isoformat()
    elif claim.deadline_at:
        deadline = timezone.localtime(claim.deadline_at).date().isoformat()

    return {
        'status': claim.status,
        'status_family': claim.status_category,
        'deadline': deadline,
        'emails_total': emails.count(),
        'emails_unresolved': emails.filter(action_required=True, auto_resolved=False).count(),
        'disputes_total': Dispute.objects.filter(claim=claim).count(),
        'next_update_due': next_update_due,
    }


def parse_alf_claim_id_from_subject(subject: str) -> Optional[str]:
    """
    Parse ALF claim ID from Zendesk ticket subject.

    Expected format: ALF followed by 7 digits (e.g., ALF1234567)
    Also handles formats with hyphens/underscores: ALF-1234567, ALF_1234567

    Args:
        subject: Zendesk ticket subject line

    Returns:
        ALF claim ID if found, None otherwise
    """
    import re

    if not subject:
        return None

    # Pattern: ALF followed by optional hyphens/underscores, then exactly 7 digits
    match = re.search(r'ALF[-_]?(\d{7})', subject, re.IGNORECASE)
    if match:
        return f"ALF{match.group(1)}"

    return None


def build_ticket_thread(data) -> dict:
    """Build the untrusted AI payload from ticket content sent by the sidebar app.

    Comments may be plain strings (legacy) or dicts {author, created_at, public,
    text}; dicts are rendered as '[created_at | author | visibility] text' lines
    so the model can reason about chronology and who said what. Caller passes
    the result as AIClient's `untrusted` (ticket content comes from external
    senders and must stay in the fenced, PII-tokenized channel).
    """
    subject = str(data.get('subject', ''))[:200]
    description = str(data.get('description', ''))[:3000]
    created_at = str(data.get('ticket_created_at', '') or '')[:40]

    raw_comments = data.get('comments') or []
    if not isinstance(raw_comments, list):
        raw_comments = [str(raw_comments)]

    lines = []
    for c in raw_comments[:30]:
        if isinstance(c, dict):
            text = str(c.get('text', '') or c.get('value', ''))[:1500].strip()
            if not text:
                continue
            when = str(c.get('created_at', ''))[:25]
            author = str(c.get('author', ''))[:80]
            visibility = 'internal note' if c.get('public') is False else 'public'
            lines.append(f"[{when} | {author} | {visibility}] {text}")
        else:
            text = str(c)[:1500].strip()
            if text:
                lines.append(text)

    untrusted = {'ticket_subject': subject, 'ticket_description': description}
    if created_at:
        untrusted['ticket_created_at'] = created_at
    if lines:
        untrusted['zendesk_comment'] = lines
    return untrusted
