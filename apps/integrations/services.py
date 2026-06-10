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
from typing import List, Dict, Any, Optional

from django.conf import settings

from apps.config.models import SystemSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Zendesk custom field IDs (populated by the marketing site for every new
# ticket).  Confirmed against the live Zendesk field list on 2026-06-10.
# When a field ID is None, the extractor falls back to LLM extraction for
# that field.
# ---------------------------------------------------------------------------
ZENDESK_FIELD_ALIAS_EMAIL: int = 13606076120860   # "Email used for submissions" (our per-case alias)
ZENDESK_FIELD_CLIENT_EMAIL: int = 13737499349020  # "Customer Email" (the client's real email)
ZENDESK_FIELD_PHONE: int = 11761070082844         # "Phone Number"
ZENDESK_FIELD_FLIGHT: int = 13737630819996        # "Flight Number"


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


def update_zendesk_ticket_status(zd_ticket_id: str, status: str) -> Optional[Dict[str, Any]]:
    """
    Update the status of a Zendesk ticket.
    
    Args:
        zd_ticket_id: The Zendesk ticket ID
        status: New status (e.g., 'open', 'pending', 'solved', 'closed')
    
    Returns:
        Ticket data dict on success, None on failure
    """
    try:
        base_url = _get_zendesk_base_url()
        headers = _get_zendesk_auth_headers()
        
        url = f"{base_url}/tickets/{zd_ticket_id}.json"
        
        payload = {
            'ticket': {
                'status': status,
            }
        }
        
        data = json.dumps(payload).encode('utf-8')
        
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method='PUT'
        )
        
        logger.info(f"Updating Zendesk ticket {zd_ticket_id} status to {status}")
        
        # Use configurable timeout
        timeout = getattr(settings, 'ZENDESK_TIMEOUT', 30)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
            ticket = result.get('ticket', {})
            
            logger.info(f"Updated Zendesk ticket {zd_ticket_id} status to {status}")
            return {
                'id': ticket.get('id'),
                'status': ticket.get('status'),
            }
            
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error updating Zendesk ticket {zd_ticket_id}: {e.code} - {error_body}")
        return None
        
    except urllib.error.URLError as e:
        logger.error(f"URL error updating Zendesk ticket {zd_ticket_id}: {e.reason}")
        return None
        
    except Exception as e:
        logger.error(f"Unexpected error updating Zendesk ticket {zd_ticket_id}: {e}")
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
        # Hard-coded custom field ID as specified
        custom_field_id = '13606076120860'
        
        # Search for tickets where the custom field contains the alias
        # Zendesk search syntax: custom_fields_{id}:"value"
        query = f'custom_fields_{custom_field_id}:"{alias}"'
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
    for field in custom_fields:
        if field.get('id') == field_id:
            value = field.get('value')
            return str(value) if value else ''
    return ''


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
        phone_structured = _get_custom_field_value(custom_fields, ZENDESK_FIELD_PHONE)
        flight_structured = _get_custom_field_value(custom_fields, ZENDESK_FIELD_FLIGHT)

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
            'flight_details': flight_structured,
            'object_description': llm_result.get('object_description', ''),
            'phone': phone_structured,
            'alternate_email': '',
        }

        logger.info(
            "Extraction completed for ticket %s (structured email=%r, flight=%r, "
            "phone=%r; LLM object_description=%r)",
            ticket_data.get('id', 'unknown'),
            bool(client_email_structured),
            bool(flight_structured),
            bool(phone_structured),
            bool(extracted['object_description']),
        )
        return extracted

    except Exception as e:
        logger.error(f"Error in extraction for Zendesk ticket: {e}", exc_info=True)
        return {
            'client_email': '',
            'flight_details': '',
            'object_description': '',
            'phone': '',
            'alternate_email': '',
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
