"""
PayPal Disputes API Client for LORA.

Provides functions for interacting with PayPal's Customer Disputes API:
- OAuth2 token management with caching
- Fetching dispute details
- Providing evidence (documents and response text)
- Accepting claims (refunds)
- Sending messages to buyers

Environment (sandbox vs live) is controlled by SystemSettings.paypal_mode and
resolved by paypal_api_base() — defaults to SANDBOX so no dispute action moves
real money until explicitly switched to live.
"""

import base64
import json
import logging
import urllib.request
import urllib.error
import uuid
from typing import Dict, Any, Optional, List, Tuple

from django.core.cache import cache
from django.conf import settings
from django.db import IntegrityError, transaction
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from apps.payments.models import (
    Dispute, DisputeDocument, DisputeActivityLog, DisputeSubmission)
from apps.config.models import SystemSettings

logger = logging.getLogger(__name__)

# PayPal REST hosts. Default to SANDBOX so no dispute action can move real
# money until SystemSettings.paypal_mode is explicitly set to 'live'.
_PAYPAL_HOSTS = {
    'sandbox': 'https://api-m.sandbox.paypal.com',
    'live': 'https://api-m.paypal.com',
}

# PayPal access-token cache TTL — slightly under the typical 30-min token life
# so a cached token is never used past its real expiry.
_PAYPAL_TOKEN_CACHE_TTL = 1500  # ~25 min

# Allowed PayPal evidence types (subset most relevant to a service concierge).
# NOTE: exact accepted values + the multipart wire format below must be
# confirmed against the PayPal sandbox before going live (Phase 1 gate).
DEFAULT_EVIDENCE_TYPE = 'PROOF_OF_FULFILLMENT'

# Default PayPal evidence_type per dispute reason. For an intangible recovery
# service, PROOF_OF_FULFILLMENT (we performed the service) is the defensible
# primary across reasons; the map lets a reason diverge without code changes and
# the manager can still override per submission. The non-PROOF_OF_FULFILLMENT
# enum values must be confirmed in the PayPal sandbox before relying on them for
# a live submission (see the wire-format caveat above).
EVIDENCE_TYPE_BY_REASON = {
    'MERCHANDISE_OR_SERVICE_NOT_RECEIVED': 'PROOF_OF_FULFILLMENT',
    'MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED': 'PROOF_OF_FULFILLMENT',
    'UNAUTHORISED': 'PROOF_OF_FULFILLMENT',
    'CREDIT_NOT_PROCESSED': 'PROOF_OF_FULFILLMENT',
    'DUPLICATE_TRANSACTION': 'PROOF_OF_FULFILLMENT',
    'INCORRECT_AMOUNT': 'PROOF_OF_FULFILLMENT',
}


def evidence_type_for_reason(reason: str) -> str:
    """PayPal evidence_type to default to for a dispute reason."""
    return EVIDENCE_TYPE_BY_REASON.get(reason or '', DEFAULT_EVIDENCE_TYPE)


# PayPal's seller-evidence API has no structured "service vs product" field, so
# we assert it in the evidence text the reviewer reads. Every INITIAL response
# (provide-evidence) leads with this fixed declaration that the transaction was
# an intangible service, not a physical product — so it can never be omitted,
# whatever the AI/manager wrote in the body. (A marker string keeps it idempotent
# if a draft already contains it.)
SERVICE_NOT_PRODUCT_MARKER = 'intangible lost-item recovery service'
SERVICE_NOT_PRODUCT_DECLARATION = (
    "This transaction was for an intangible lost-item recovery service, not a "
    "physical product. No goods were sold or shipped; the customer paid a service "
    "fee for the search and recovery work we performed on their behalf. Please "
    "assess this dispute as a service, not as merchandise."
)


def _lead_with_service_declaration(notes: str) -> str:
    """Prepend the service-not-product declaration to evidence notes, unless the
    text already states it (idempotent)."""
    body = (notes or '').strip()
    if SERVICE_NOT_PRODUCT_MARKER in body.lower():
        return body
    return f"{SERVICE_NOT_PRODUCT_DECLARATION}\n\n{body}" if body else SERVICE_NOT_PRODUCT_DECLARATION


def paypal_api_base() -> str:
    """Base REST URL for the configured PayPal environment (sandbox by default)."""
    mode = (SystemSettings.get_instance().paypal_mode or 'sandbox').strip().lower()
    return _PAYPAL_HOSTS.get(mode, _PAYPAL_HOSTS['sandbox'])


def _encode_multipart(input_json: dict, files: List[dict]):
    """Build a multipart/form-data body for PayPal's provide-evidence call.

    files: [{'name','filename','content'(bytes),'content_type'}]. Returns
    (body_bytes, content_type_header). PayPal expects a JSON 'input' part
    describing the evidences plus one part per uploaded file.
    """
    boundary = f"----LORA{uuid.uuid4().hex}"
    crlf = b'\r\n'
    parts = []

    parts.append(b'--' + boundary.encode())
    parts.append(b'Content-Disposition: form-data; name="input"')
    # Declare the charset so strict multipart parsers read non-ASCII notes
    # (e.g. accented buyer names) correctly — the JSON below is utf-8 encoded.
    parts.append(b'Content-Type: application/json; charset=utf-8')
    parts.append(b'')
    parts.append(json.dumps(input_json).encode('utf-8'))

    for f in files:
        parts.append(b'--' + boundary.encode())
        parts.append(
            f'Content-Disposition: form-data; name="{f["name"]}"; '
            f'filename="{f["filename"]}"'.encode('utf-8'))
        parts.append(f'Content-Type: {f.get("content_type", "application/pdf")}'.encode('utf-8'))
        parts.append(b'')
        parts.append(f['content'])

    parts.append(b'--' + boundary.encode() + b'--')
    parts.append(b'')
    body = crlf.join(parts)
    return body, f'multipart/form-data; boundary={boundary}'


# NB: no @retry here — this function catches HTTPError/URLError/Exception and
# returns None, so tenacity would never see an exception to retry on (the
# decorator was inert dead code and was removed). The token is cached for 25 min,
# so a transient fetch failure is rare and callers already handle None.
def get_paypal_access_token() -> Optional[str]:
    """
    Get OAuth2 access token from PayPal with caching.

    Retrieves client credentials from SystemSettings and obtains an OAuth2
    access token from PayPal. The token is cached for 25 minutes (slightly
    less than the typical 30-minute TTL) to avoid repeated API calls.

    Returns:
        Access token string if successful, None on failure.
    """
    try:
        # Get PayPal credentials from SystemSettings
        system_settings = SystemSettings.get_instance()
        client_id = system_settings.paypal_client_id
        secret = system_settings.paypal_secret

        if not client_id or not secret:
            logger.error("PayPal credentials not configured in SystemSettings")
            return None

        # Cache key based on client_id to support multiple configurations
        cache_key = f'paypal_access_token_{client_id}'

        # Try to get token from cache first
        access_token = cache.get(cache_key)
        if access_token:
            logger.debug("PayPal access token retrieved from cache")
            return access_token

        # Build OAuth2 token request
        base_url = paypal_api_base()
        token_url = f"{base_url}/v1/oauth2/token"

        # Encode credentials for Basic auth
        credentials = f"{client_id}:{secret}"
        encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')

        # Create token request
        token_request = urllib.request.Request(
            token_url,
            data=b'grant_type=client_credentials',
            headers={
                'Authorization': f'Basic {encoded_credentials}',
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            method='POST'
        )

        # Use configurable timeout from settings
        timeout = getattr(settings, 'PAYPAL_TIMEOUT', 30)

        with urllib.request.urlopen(token_request, timeout=timeout) as response:
            token_data = json.loads(response.read().decode('utf-8'))
            access_token = token_data.get('access_token')

            if access_token:
                # Cache token for ~25 minutes (slightly less than the typical
                # 30-minute TTL); see _PAYPAL_TOKEN_CACHE_TTL.
                cache.set(cache_key, access_token, timeout=_PAYPAL_TOKEN_CACHE_TTL)
                logger.info("PayPal access token obtained and cached for 25 minutes")
                return access_token
            else:
                logger.error("No access_token in PayPal OAuth2 response")
                return None

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error getting PayPal access token: {e.code} - {error_body}")
        return None

    except urllib.error.URLError as e:
        logger.error(f"URL error getting PayPal access token: {e.reason}")
        return None

    except Exception as e:
        logger.error(f"Unexpected error getting PayPal access token: {e}")
        return None


def paypal_json_request(url: str, *, access_token: str, method: str,
                        payload: Any = None, extra_headers: Optional[Dict[str, str]] = None,
                        timeout: Optional[int] = None) -> Any:
    """Build + send a PayPal REST JSON request and return the parsed JSON body.

    Centralises the Bearer-auth + JSON-encode + urlopen + parse boilerplate the
    dispute/refund JSON endpoints repeat. RAISES on transport/parse errors so each
    caller keeps its own except ladder and return value. Per-call headers (e.g. a
    PayPal-Request-Id idempotency key) go in extra_headers.

    Content-Type 'application/json' is set ONLY when there is a payload (POST), to
    match the existing call sites (the GET status lookup sends Authorization only).
    `timeout` defaults to settings.PAYPAL_TIMEOUT (the dispute endpoints' existing
    behaviour); the refund endpoints pass timeout=30 explicitly to stay byte-identical
    to their previous hard-coded value.
    """
    headers = {'Authorization': f'Bearer {access_token}'}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode('utf-8')
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t = timeout if timeout is not None else getattr(settings, 'PAYPAL_TIMEOUT', 30)
    with urllib.request.urlopen(req, timeout=t) as response:
        return json.loads(response.read().decode('utf-8'))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((urllib.error.HTTPError, urllib.error.URLError))
)
def fetch_dispute_details(dispute_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch full dispute details from PayPal.

    Makes a GET request to /v1/customer-disputes/{dispute_id} to retrieve
    complete dispute information including buyer details, transaction info,
    dispute reason, amount, and current status.

    Args:
        dispute_id: The PayPal dispute ID (e.g., PP-D-XXXXX)

    Returns:
        Dictionary with dispute details if successful, None on failure.
        The returned dict contains fields like:
        - dispute_id: PayPal dispute identifier
        - case_id: PayPal case identifier
        - reason: Dispute reason code
        - status: Current dispute status
        - amount: Dispute amount object
        - buyer: Buyer information
        - transaction: Transaction details
        - create_time: When dispute was created
        - update_time: Last update time
    """
    access_token = get_paypal_access_token()
    if not access_token:
        logger.error(f"Cannot fetch dispute {dispute_id}: no access token")
        return None

    try:
        base_url = paypal_api_base()
        dispute_url = f"{base_url}/v1/customer/disputes/{dispute_id}"

        # Create request with proper headers
        request = urllib.request.Request(
            dispute_url,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            },
            method='GET'
        )

        # Use configurable timeout
        timeout = getattr(settings, 'PAYPAL_TIMEOUT', 30)

        with urllib.request.urlopen(request, timeout=timeout) as response:
            dispute_data = json.loads(response.read().decode('utf-8'))
            logger.info(f"Successfully fetched dispute details for {dispute_id}")
            return dispute_data

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error fetching dispute {dispute_id}: {e.code} - {error_body}")
        return None

    except urllib.error.URLError as e:
        logger.error(f"URL error fetching dispute {dispute_id}: {e.reason}")
        return None

    except Exception as e:
        logger.error(f"Unexpected error fetching dispute {dispute_id}: {e}")
        return None


def list_paypal_disputes(page_size: int = 50, max_pages: int = 20,
                         include_resolved: bool = False) -> List[str]:
    """Return the dispute IDs PayPal currently holds for this account.

    Calls GET /v1/customer/disputes (paginated via the `next` HATEOAS link) and
    collects each dispute_id. Used to BACKFILL disputes that predate the webhook
    subscription — the webhook only delivers events from when it goes live, so
    pre-existing disputes must be pulled with this list call.

    PayPal's list returns the last ~180 days, OPEN and CLOSED alike. By default
    we SKIP already-resolved/closed disputes (status or dispute_state RESOLVED) —
    they need no action and would only clutter the workbench. Pass
    include_resolved=True to get everything. Returns [] on any failure / missing
    Disputes-API permission; callers treat empty as "nothing to pull or couldn't
    read".
    """
    access_token = get_paypal_access_token()
    if not access_token:
        logger.error("Cannot list disputes: no PayPal access token")
        return []

    base_url = paypal_api_base()
    url = f"{base_url}/v1/customer/disputes?page_size={page_size}"
    headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
    timeout = getattr(settings, 'PAYPAL_TIMEOUT', 30)

    dispute_ids: List[str] = []
    seen_urls = set()
    for _ in range(max_pages):
        if not url or url in seen_urls:
            break
        seen_urls.add(url)
        try:
            request = urllib.request.Request(url, headers=headers, method='GET')
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ''
            logger.error(f"HTTP error listing disputes: {e.code} - {error_body}")
            break
        except Exception as e:
            logger.error(f"Error listing disputes: {e}")
            break

        for item in (data.get('items') or []):
            if not include_resolved:
                state = (item.get('dispute_state') or '').upper()
                item_status = (item.get('status') or '').upper()
                if state == 'RESOLVED' or item_status == 'RESOLVED':
                    continue  # skip closed/resolved disputes
            did = item.get('dispute_id') or item.get('id')
            if did:
                dispute_ids.append(str(did))

        # Follow the HATEOAS `next` link for the next page.
        url = None
        for link in (data.get('links') or []):
            if link.get('rel') == 'next' and link.get('href'):
                url = link['href']
                break

    return list(dict.fromkeys(dispute_ids))  # dedupe, preserve order


# No @retry here: this swallows HTTPError/URLError and returns False, so a retry
# decorator never fired anyway — and accepting a claim moves money. Double-refund is
# now guarded two ways: a local status pre-check (skips if already accepted/resolved)
# and a stable PayPal-Request-Id idempotency key on the POST. The caller still
# re-syncs status from PayPal before advising any manual retry.
def accept_claim(dispute_id: str, note: str = '') -> bool:
    """
    Accept a dispute claim (issue refund) via PayPal API.

    Makes a POST request to /v1/customer-disputes/{dispute_id}/accept-claim
    to accept the dispute and issue a refund to the buyer.

    Args:
        dispute_id: The PayPal dispute ID
        note: Optional note to include with the acceptance

    Returns:
        True if claim was successfully accepted, False on failure.
        On success, updates the local Dispute status to ACCEPTED.
    """
    access_token = get_paypal_access_token()
    if not access_token:
        logger.error(f"Cannot accept claim for dispute {dispute_id}: no access token")
        return False

    try:
        # Get the local Dispute record
        try:
            dispute = Dispute.objects.get(paypal_dispute_id=dispute_id)
        except Dispute.DoesNotExist:
            logger.error(f"Local Dispute record not found for PayPal dispute {dispute_id}")
            return False

        # Idempotency guard: accepting a claim issues a refund. If the dispute is
        # already accepted/resolved, accepting again would move money a second time —
        # bail without re-POSTing. The stable PayPal-Request-Id below dedups a race.
        if dispute.status in (
            Dispute.STATUS_ACCEPTED,
            Dispute.STATUS_RESOLVED_WON,
            Dispute.STATUS_RESOLVED_LOST,
        ):
            logger.info(
                f"Dispute {dispute_id} already {dispute.status}; skipping accept-claim "
                f"to avoid a duplicate refund"
            )
            return True

        base_url = paypal_api_base()
        accept_url = f"{base_url}/v1/customer/disputes/{dispute_id}/accept-claim"

        # Build acceptance payload
        accept_payload = {}
        if note:
            accept_payload["note"] = note

        # Stable idempotency key so a retried accept-claim is deduplicated
        # PayPal-side and the refund cannot be issued twice.
        result = paypal_json_request(
            accept_url, access_token=access_token, method='POST',
            payload=accept_payload,
            extra_headers={'PayPal-Request-Id': f'accept-claim-{dispute_id}'})
        logger.info(f"Successfully accepted claim for dispute {dispute_id}")

        # External call done — persist local state in its own short transaction.
        with transaction.atomic():
            dispute.status = Dispute.STATUS_ACCEPTED
            dispute.save(update_fields=['status'])
            DisputeActivityLog.objects.create(
                dispute=dispute,
                action=DisputeActivityLog.ACTION_DISPUTE_RESOLVED,
                details=f"Claim accepted via PayPal API. Refund issued. Note: {note[:200] if note else 'None'}",
            )

        logger.info(f"Updated Dispute #{dispute.id} status to ACCEPTED")
        return True

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error accepting claim for dispute {dispute_id}: {e.code} - {error_body}")
        return False

    except urllib.error.URLError as e:
        logger.error(f"URL error accepting claim for dispute {dispute_id}: {e.reason}")
        return False

    except Exception as e:
        logger.error(f"Unexpected error accepting claim for dispute {dispute_id}: {e}")
        return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((urllib.error.HTTPError, urllib.error.URLError))
)
def send_message(dispute_id: str, message: str) -> bool:
    """
    Send a message to the buyer via PayPal.

    Makes a POST request to /v1/customer-disputes/{dispute_id}/message
    to send a message to the buyer regarding the dispute.

    Args:
        dispute_id: The PayPal dispute ID
        message: The message content to send to the buyer

    Returns:
        True if message was successfully sent, False on failure.
        On success, logs the message to DisputeActivityLog.
    """
    access_token = get_paypal_access_token()
    if not access_token:
        logger.error(f"Cannot send message for dispute {dispute_id}: no access token")
        return False

    if not message or not message.strip():
        logger.error("Cannot send empty message")
        return False

    try:
        # Get the local Dispute record
        try:
            dispute = Dispute.objects.get(paypal_dispute_id=dispute_id)
        except Dispute.DoesNotExist:
            logger.error(f"Local Dispute record not found for PayPal dispute {dispute_id}")
            return False

        base_url = paypal_api_base()
        message_url = f"{base_url}/v1/customer/disputes/{dispute_id}/send-message"

        # Build message payload
        message_payload = {
            "message": message
        }

        result = paypal_json_request(
            message_url, access_token=access_token, method='POST',
            payload=message_payload)
        logger.info(f"Successfully sent message for dispute {dispute_id}")

        # External call done — log the activity in its own short transaction.
        with transaction.atomic():
            DisputeActivityLog.objects.create(
                dispute=dispute,
                action=DisputeActivityLog.ACTION_NOTE_ADDED,
                details=f"Message sent to buyer via PayPal API. Message length: {len(message)} chars",
            )

        logger.info(f"Logged message activity for Dispute #{dispute.id}")
        return True

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error sending message for dispute {dispute_id}: {e.code} - {error_body}")
        return False

    except urllib.error.URLError as e:
        logger.error(f"URL error sending message for dispute {dispute_id}: {e.reason}")
        return False

    except Exception as e:
        logger.error(f"Unexpected error sending message for dispute {dispute_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Back-and-forth submissions: a generic multipart transport + the orchestration
# that auto-picks the endpoint, records a DisputeSubmission, and re-syncs.
# ---------------------------------------------------------------------------

def _post_dispute_action_multipart(dispute_id: str, action: str,
                                   input_json: dict, files: List[dict]):
    """POST a multipart input+files body to .../{dispute_id}/{action}.

    Transport ONLY — no DB writes, NOT auto-retried (a submit that may have
    partly landed must not be blindly re-POSTed). Returns (ok: bool,
    response: dict|None) — the response dict (or a structured error) is stored
    on the DisputeSubmission for audit.
    """
    access_token = get_paypal_access_token()
    if not access_token:
        logger.error(f"Cannot POST {action} for dispute {dispute_id}: no access token")
        return False, {'error': 'no_access_token'}

    url = f"{paypal_api_base()}/v1/customer/disputes/{dispute_id}/{action}"
    body, content_type = _encode_multipart(input_json, files or [])
    timeout = getattr(settings, 'PAYPAL_TIMEOUT', 30)
    try:
        request = urllib.request.Request(
            url, data=body,
            headers={'Authorization': f'Bearer {access_token}', 'Content-Type': content_type},
            method='POST')
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8')
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {'raw': raw[:1000]}
        logger.info(f"PayPal {action} OK for dispute {dispute_id} ({len(files or [])} file(s))")
        return True, data
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error on {action} for dispute {dispute_id}: {e.code} - {error_body}")
        return False, {'error': 'http_error', 'code': e.code, 'body': error_body[:1000]}
    except urllib.error.URLError as e:
        logger.error(f"URL error on {action} for dispute {dispute_id}: {e.reason}")
        return False, {'error': 'url_error', 'reason': str(e.reason)}
    except Exception as e:
        logger.error(f"Unexpected error on {action} for dispute {dispute_id}: {e}")
        return False, {'error': 'unexpected', 'detail': str(e)[:500]}


def provide_supporting_info(dispute_id: str, notes: str,
                            files: Optional[List[dict]] = None) -> Tuple[bool, Optional[dict]]:
    """Add supporting info to a dispute already UNDER PayPal review — the
    back-and-forth follow-up channel. Multipart: a JSON `input` part
    {"notes": ...} plus optional document files. Transport only; returns
    (ok, response)."""
    return _post_dispute_action_multipart(
        dispute_id, 'provide-supporting-info', {"notes": notes or ''}, files)


def provide_evidence_files(dispute_id: str, notes: str, files: Optional[List[dict]] = None,
                           evidence_type: str = DEFAULT_EVIDENCE_TYPE) -> Tuple[bool, Optional[dict]]:
    """First seller response (provide-evidence) carrying a GENERIC files list, so
    a submission can include the evidence-report PDF and the manager's images
    together. This is the path submit_dispute_response uses. Transport only;
    returns (ok, response)."""
    input_json = {"evidences": [{
        "evidence_type": evidence_type or DEFAULT_EVIDENCE_TYPE,
        # Always lead with the service-not-product declaration on the first
        # response (PayPal has no structured field for it).
        "notes": _lead_with_service_declaration(notes),
        "document_ids": [f['filename'] for f in (files or [])],
    }]}
    return _post_dispute_action_multipart(dispute_id, 'provide-evidence', input_json, files)


_IMAGE_CONTENT_TYPES = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'gif': 'image/gif', 'webp': 'image/webp', 'pdf': 'application/pdf',
}


def _read_file_field(field, default_ct: str = 'application/octet-stream'):
    """Read a Django File/Image field into a multipart-ready dict, or None if it
    can't be read. content_type inferred from the extension."""
    if not field:
        return None
    try:
        field.open('rb')
        content = field.read()
    except Exception as e:
        logger.error(f"Could not read file {getattr(field, 'name', '?')}: {e}")
        return None
    name = field.name.split('/')[-1]
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    return {'name': name, 'filename': name, 'content': content,
            'content_type': _IMAGE_CONTENT_TYPES.get(ext, default_ct)}


def _build_submission_files(submission: DisputeSubmission) -> List[dict]:
    """The files to upload with a submission: the manager's images, plus the
    latest evidence-report PDF when attach_evidence_pdf is ticked, plus the
    stored Terms & Conditions PDF when attach_terms is ticked. Unreadable files
    are skipped (logged), never crashing the submit."""
    files = []
    if submission.attach_evidence_pdf:
        doc = (DisputeDocument.objects
               .filter(dispute=submission.dispute,
                       doc_type=DisputeDocument.DOC_TYPE_EVIDENCE_REPORT)
               .exclude(file_path='').order_by('-created_at').first())
        if doc:
            f = _read_file_field(doc.file_path, default_ct='application/pdf')
            if f:
                files.append(f)
        else:
            logger.warning(f"attach_evidence_pdf set but no evidence-report PDF found "
                           f"for dispute #{submission.dispute_id}")
    if getattr(submission, 'attach_terms', False):
        from apps.config.models import SystemSettings
        terms = SystemSettings.get_instance().terms_conditions_pdf
        if terms:
            f = _read_file_field(terms, default_ct='application/pdf')
            if f:
                files.append(f)
        else:
            logger.warning(f"attach_terms set but no Terms & Conditions PDF uploaded "
                           f"(dispute #{submission.dispute_id})")
    for img in submission.images.all():
        f = _read_file_field(img.file)
        if f:
            files.append(f)
    return files


def _record_submission_outcome(submission: DisputeSubmission, *, status, performed_by,
                               response, action='') -> None:
    """Persist a submission's terminal state + an activity-log line, in one txn."""
    from django.utils import timezone
    with transaction.atomic():
        fields = ['status', 'paypal_response', 'updated_at']
        submission.status = status
        # Only record the submitter when we actually have one — never NULL out a
        # previously-recorded human submitter on a system/automated re-record.
        if performed_by is not None:
            submission.submitted_by = performed_by
            fields.append('submitted_by')
        submission.paypal_response = response or {}
        if status == DisputeSubmission.STATUS_SUBMITTED:
            submission.submitted_at = timezone.now()
            fields.append('submitted_at')
        submission.save(update_fields=fields)
        if status == DisputeSubmission.STATUS_SUBMITTED:
            details = (f"Submitted to PayPal via {action} (submission #{submission.id}).")
            log_action = DisputeActivityLog.ACTION_EVIDENCE_SENT
        else:
            details = (f"PayPal submission #{submission.id} FAILED ({action or 'no endpoint'}): "
                       f"{str(response)[:300]}")
            log_action = DisputeActivityLog.ACTION_NOTE_ADDED
        DisputeActivityLog.objects.create(
            dispute=submission.dispute, action=log_action,
            performed_by=performed_by, details=details)


def submit_dispute_response(submission: DisputeSubmission, *, performed_by=None) -> bool:
    """Submit a prepared DisputeSubmission to PayPal.

    Auto-picks the endpoint from the dispute's current state (provide-evidence
    for the first response, provide-supporting-info once it's under review),
    uploads the chosen attachments, records the outcome on the submission +
    activity log, and re-syncs the dispute from PayPal afterwards (so its state
    and evidence history update). Returns True on success; on failure marks the
    submission FAILED and returns False so the manager can edit and retry.
    """
    dispute = submission.dispute
    endpoint = dispute.submit_endpoint
    if not endpoint:
        logger.warning(f"Dispute #{dispute.id} has no available PayPal submit endpoint.")
        _record_submission_outcome(submission, status=DisputeSubmission.STATUS_FAILED,
                                   performed_by=performed_by,
                                   response={'error': 'no_submit_endpoint'}, action='')
        return False

    files = _build_submission_files(submission)

    if endpoint == 'provide-evidence':
        ok, response = provide_evidence_files(
            dispute.paypal_dispute_id, submission.notes, files,
            evidence_type=submission.evidence_type or evidence_type_for_reason(dispute.dispute_reason))
        submission.kind = DisputeSubmission.KIND_EVIDENCE
    else:
        ok, response = provide_supporting_info(dispute.paypal_dispute_id, submission.notes, files)
        submission.kind = DisputeSubmission.KIND_SUPPORTING_INFO
    submission.save(update_fields=['kind', 'updated_at'])

    if not ok:
        _record_submission_outcome(submission, status=DisputeSubmission.STATUS_FAILED,
                                   performed_by=performed_by,
                                   response=response, action=endpoint)
        return False

    _record_submission_outcome(submission, status=DisputeSubmission.STATUS_SUBMITTED,
                               performed_by=performed_by,
                               response=response, action=endpoint)
    # Re-sync OUTSIDE the DB transaction (network I/O): refresh state + evidences[].
    try:
        sync_dispute_from_paypal(dispute.paypal_dispute_id)
    except Exception as e:
        logger.warning(f"Post-submit re-sync failed for dispute {dispute.paypal_dispute_id}: {e}")
    return True


# ---------------------------------------------------------------------------
# Phase 2 — inbound: verify PayPal's signature, then ingest the dispute
# ---------------------------------------------------------------------------

def verify_webhook_signature(request_headers, event: dict) -> bool:
    """Verify a PayPal webhook is genuinely from PayPal.

    Uses PayPal's verify-webhook-signature API with the transmission headers
    PayPal sent + our configured webhook id. Fail-closed: any error, a missing
    webhook id, or a non-SUCCESS verdict returns False (reject the event).
    """
    webhook_id = (SystemSettings.get_instance().paypal_webhook_id or '').strip()
    if not webhook_id:
        logger.error("Cannot verify PayPal webhook: paypal_webhook_id not configured")
        return False
    access_token = get_paypal_access_token()
    if not access_token:
        return False

    def h(name):
        return request_headers.get(name, '')

    payload = {
        'auth_algo': h('Paypal-Auth-Algo'),
        'cert_url': h('Paypal-Cert-Url'),
        'transmission_id': h('Paypal-Transmission-Id'),
        'transmission_sig': h('Paypal-Transmission-Sig'),
        'transmission_time': h('Paypal-Transmission-Time'),
        'webhook_id': webhook_id,
        'webhook_event': event,
    }
    url = f"{paypal_api_base()}/v1/notifications/verify-webhook-signature"
    try:
        result = paypal_json_request(
            url, access_token=access_token, method='POST', payload=payload)
        verified = result.get('verification_status') == 'SUCCESS'
        if not verified:
            logger.warning(f"PayPal webhook signature verification: {result.get('verification_status')}")
        return verified
    except Exception as e:
        logger.error(f"Error verifying PayPal webhook signature: {e}")
        return False


def _parse_paypal_time(value):
    """Parse a PayPal ISO timestamp; None on failure."""
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def _match_claim_for_dispute(details: dict):
    """Find the LORA Claim a PayPal dispute belongs to.

    PayPal does NOT include the buyer's email in the dispute object (the `buyer`
    only has a name + payer_id), so matching by email never works. The reliable
    identifiers live on disputed_transactions[0]:
      1. invoice_number — our merchant invoice, which embeds the ALF claim id
         (e.g. "ccbfae-ALF7410846") → Claim.alf_claim_id (primary). When the
         claim AND the dispute both carry a PayPal transaction id, they must
         AGREE (double-verification); a mismatch refuses the ALF link;
      2. transaction id (seller/buyer) → Claim.paypal_transaction_id — the
         authoritative unique key; also resolves an ALF-vs-txn disagreement;
      3. custom — the WooCommerce order id → Claim.woocommerce_id (fallback);
      4. buyer.email — only if PayPal ever does provide it (it usually doesn't).
    Returns a Claim or None.

    N+1 note: this runs up to ~4 Claim.objects lookups per dispute. That's fine —
    it's called once per dispute on the webhook/backfill path (bounded by dispute
    count, not row count); don't promote it to a per-row loop without batching the
    identifier lookups first.
    """
    from apps.claims.models import Claim
    from apps.integrations.services import parse_alf_claim_id_from_subject

    txns = details.get('disputed_transactions') or [{}]
    txn = txns[0] if txns else {}

    # PayPal transaction ids on the dispute (we store seller_transaction_id, but
    # check both so a claim populated with either id still matches).
    dispute_txns = {t for t in (txn.get('seller_transaction_id'),
                                txn.get('buyer_transaction_id')) if t}

    # 1. ALF claim id from the invoice (primary key).
    alf_id = parse_alf_claim_id_from_subject(txn.get('invoice_number') or '')
    if alf_id:
        claim = Claim.objects.filter(alf_claim_id__iexact=alf_id).first()
        if claim:
            claim_txn = (claim.paypal_transaction_id or '').strip()
            # Double-verification: when BOTH sides carry a transaction id they
            # must agree. A disagreement is suspicious — don't auto-link on the
            # ALF number; fall through (a transaction-id match below may resolve
            # it, else it stays unmatched for manual review).
            if claim_txn and dispute_txns:
                if claim_txn in dispute_txns:
                    return claim                      # ALF + transaction id agree
                logger.warning(
                    f"Dispute/claim transaction-id mismatch for {alf_id}: claim has "
                    f"{claim_txn}, dispute has {sorted(dispute_txns)} — not auto-linking by ALF")
            else:
                return claim                          # ALF alone (nothing to cross-check)

    # 2. Transaction id (authoritative unique key; also resolves an ALF mismatch).
    if dispute_txns:
        claim = Claim.objects.filter(paypal_transaction_id__in=dispute_txns).first()
        if claim:
            return claim

    # 3. WooCommerce order id in the transaction's `custom` field.
    custom = (txn.get('custom') or '').strip()
    if custom:
        claim = Claim.objects.filter(woocommerce_id=custom).first()
        if claim:
            return claim

    # 4. Buyer email, if PayPal ever provides it (it usually doesn't).
    buyer = txn.get('buyer') or details.get('buyer') or {}
    buyer_email = (buyer.get('email') or '').strip().lower()
    if buyer_email:
        claim = Claim.objects.filter(client_email__iexact=buyer_email).first()
        if claim:
            return claim

    return None


def ingest_dispute(dispute_id: str, raw_event: dict = None):
    """Fetch a dispute from PayPal and create the local Dispute (idempotent).

    Matches to a Claim via _match_claim_for_dispute (invoice ALF id / WooCommerce
    order id — NOT buyer email, which PayPal doesn't send) and captures the
    response deadline. Returns (dispute, created); for an already-stored dispute
    returns (existing, False) and re-matches it if it was previously unmatched
    (self-heal for rows pulled before the matching fix). (None, False) if PayPal
    couldn't be read.
    """
    from django.utils import timezone

    existing = Dispute.objects.filter(paypal_dispute_id=dispute_id).first()
    if existing:
        # Self-heal: rows pulled before the matching fix are unmatched (we used
        # to match by buyer email, which PayPal never provides). Retry now.
        if existing.claim_id is None:
            details = fetch_dispute_details(dispute_id)
            claim = _match_claim_for_dispute(details) if details else None
            if claim:
                existing.claim = claim
                if not existing.zd_ticket_id and claim.zd_ticket_id:
                    existing.zd_ticket_id = claim.zd_ticket_id
                if existing.status == Dispute.STATUS_RECEIVED:
                    existing.status = Dispute.STATUS_MATCHED
                existing.save(update_fields=['claim', 'zd_ticket_id', 'status', 'updated_at'])
                DisputeActivityLog.objects.create(
                    dispute=existing, action=DisputeActivityLog.ACTION_DISPUTE_MATCHED,
                    details=f"Matched to claim #{claim.id} on re-pull (invoice/order reference).")
        return existing, False

    details = fetch_dispute_details(dispute_id)
    if not details:
        logger.error(f"Cannot ingest dispute {dispute_id}: PayPal returned no details")
        return None, False

    txns = details.get('disputed_transactions') or [{}]
    txn = txns[0] if txns else {}
    buyer = txn.get('buyer') or details.get('buyer') or {}
    buyer_email = (buyer.get('email') or txn.get('buyer_email') or '').strip().lower()
    buyer_name = buyer.get('name') or txn.get('buyer_name') or ''

    amount = details.get('dispute_amount') or {}
    reason = (details.get('reason') or '').strip()

    claim = _match_claim_for_dispute(details)

    # Atomic create guarded against the check-then-create race: the manual pull
    # (dispute_pull_from_paypal) calls this in a loop with no ProcessedWebhookEvent
    # gate, so two concurrent pulls — or a pull racing a webhook — can both pass
    # the existence check above and the second create() hits the unique
    # paypal_dispute_id constraint. Adopt the winning row idempotently instead of
    # raising. The savepoint keeps any enclosing transaction usable for re-fetch.
    try:
        with transaction.atomic():
            dispute = Dispute.objects.create(
                paypal_dispute_id=dispute_id,
                paypal_case_id=details.get('case_id', '') or '',
                claim=claim,
                zd_ticket_id=(claim.zd_ticket_id if claim else ''),
                status=Dispute.STATUS_MATCHED if claim else Dispute.STATUS_RECEIVED,
                # Only set reason if it matches our enum; Phase 4 fixes the enum
                # (incl. British UNAUTHORISED). The raw reason is kept in the payload.
                dispute_reason=reason if reason in Dispute.VALID_REASONS else '',
                dispute_amount=amount.get('value') or None,
                dispute_currency=(amount.get('currency_code') or '')[:3],
                buyer_email=buyer_email,  # '' allowed when claim is None (model constraint)
                buyer_name=buyer_name[:255],
                transaction_id=txn.get('seller_transaction_id', '') or '',
                transaction_date=(_parse_paypal_time(txn.get('create_time'))
                                  or _parse_paypal_time(details.get('create_time'))
                                  or timezone.now()),
                seller_response_due=_parse_paypal_time(details.get('seller_response_due_date')),
                dispute_life_cycle_stage=details.get('dispute_life_cycle_stage', '') or '',
                raw_webhook_payload=details,
            )
    except IntegrityError:
        existing = Dispute.objects.filter(paypal_dispute_id=dispute_id).first()
        if existing is None:
            raise
        logger.info(f"Dispute {dispute_id} created concurrently; adopting existing #{existing.id}")
        return existing, False
    DisputeActivityLog.objects.create(
        dispute=dispute, action=DisputeActivityLog.ACTION_DISPUTE_CREATED,
        details=f"Ingested from PayPal ({details.get('dispute_life_cycle_stage', '?')} stage, "
                f"reason={reason or '?'}). {'Matched claim #%s' % claim.id if claim else 'No claim matched'}.")
    if claim:
        DisputeActivityLog.objects.create(
            dispute=dispute, action=DisputeActivityLog.ACTION_DISPUTE_MATCHED,
            details=f"Matched to claim #{claim.id} by invoice/order reference.")
    logger.info(f"Ingested dispute {dispute_id} (claim={'#%s' % claim.id if claim else 'none'})")
    return dispute, True


def sync_dispute_from_paypal(dispute_id: str):
    """Refresh a local Dispute from PayPal (Phase 3 — UPDATED/RESOLVED events).

    Updates stage, deadline, amount and reason; on a RESOLVED dispute maps
    PayPal's outcome to RESOLVED_WON / RESOLVED_LOST. Does NOT clobber the
    human workflow status (DOCUMENTS_READY etc.) on a mere UPDATE — only a
    resolution changes the LORA status. Returns the Dispute or None.
    """
    from django.utils import timezone

    dispute = Dispute.objects.filter(paypal_dispute_id=dispute_id).first()
    if dispute is None:
        # Unknown dispute updated before we ever ingested it — ingest now.
        dispute, _ = ingest_dispute(dispute_id)
        if dispute is None:
            # PayPal unreachable — signal failure so the webhook releases its
            # idempotency gate and 503s for a retry (don't mark it "processed").
            raise RuntimeError(f"Could not ingest dispute {dispute_id}: PayPal unreachable")
        return dispute

    details = fetch_dispute_details(dispute_id)
    if not details:
        # Couldn't fetch — RAISE rather than return quietly, so a webhook caller
        # doesn't mark the event processed without actually syncing.
        raise RuntimeError(f"Could not fetch dispute {dispute_id} from PayPal to sync")

    update_fields = []
    stage = details.get('dispute_life_cycle_stage', '') or ''
    if stage and stage != dispute.dispute_life_cycle_stage:
        dispute.dispute_life_cycle_stage = stage
        update_fields.append('dispute_life_cycle_stage')
    due = _parse_paypal_time(details.get('seller_response_due_date'))
    if due and due != dispute.seller_response_due:
        dispute.seller_response_due = due
        update_fields.append('seller_response_due')
    reason = (details.get('reason') or '').strip()
    if reason in Dispute.VALID_REASONS and reason != dispute.dispute_reason:
        dispute.dispute_reason = reason
        update_fields.append('dispute_reason')

    # PayPal reports closure in either key — treat both as RESOLVED so a
    # state-only payload still becomes terminal locally.
    pp_status = (details.get('status') or '').upper()
    pp_state = (details.get('dispute_state') or '').upper()
    if pp_status == 'RESOLVED' or pp_state == 'RESOLVED':
        outcome = (details.get('dispute_outcome') or {}).get('outcome_code', '') or ''
        won = 'SELLER' in outcome.upper()  # e.g. RESOLVED_SELLER_FAVOUR
        new_status = Dispute.STATUS_RESOLVED_WON if won else Dispute.STATUS_RESOLVED_LOST
        if dispute.status != new_status:
            dispute.status = new_status
            update_fields.append('status')
            DisputeActivityLog.objects.create(
                dispute=dispute, action=DisputeActivityLog.ACTION_DISPUTE_RESOLVED,
                details=f"PayPal resolved the dispute: {new_status} (outcome={outcome or '?'}).")

    # Always refresh the stored raw payload so the list filter (needs-action vs
    # under-review), the Raw-PayPal viewer, and the resolved-prune reflect the
    # dispute's CURRENT state, not its state at first ingest.
    if details != dispute.raw_webhook_payload:
        dispute.raw_webhook_payload = details
        update_fields.append('raw_webhook_payload')

    if update_fields:
        dispute.save(update_fields=list(set(update_fields)) + ['updated_at'])
        logger.info(f"Synced dispute {dispute_id}: {update_fields}")
    return dispute
