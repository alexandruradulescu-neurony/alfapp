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
import socket
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List

from django.core.cache import cache
from django.conf import settings
from django.db import transaction
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from apps.payments.models import Dispute, DisputeDocument, DisputeActivityLog
from apps.config.models import SystemSettings

logger = logging.getLogger(__name__)

# PayPal REST hosts. Default to SANDBOX so no dispute action can move real
# money until SystemSettings.paypal_mode is explicitly set to 'live'.
_PAYPAL_HOSTS = {
    'sandbox': 'https://api-m.sandbox.paypal.com',
    'live': 'https://api-m.paypal.com',
}

# Allowed PayPal evidence types (subset most relevant to a service concierge).
# NOTE: exact accepted values + the multipart wire format below must be
# confirmed against the PayPal sandbox before going live (Phase 1 gate).
DEFAULT_EVIDENCE_TYPE = 'PROOF_OF_FULFILLMENT'


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
    import uuid
    boundary = f"----LORA{uuid.uuid4().hex}"
    crlf = b'\r\n'
    parts = []

    parts.append(b'--' + boundary.encode())
    parts.append(b'Content-Disposition: form-data; name="input"')
    parts.append(b'Content-Type: application/json')
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


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((
        urllib.error.HTTPError,
        urllib.error.URLError,
        socket.timeout,
        ConnectionResetError,
        ConnectionError,
    ))
)
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
                # Cache token for 25 minutes (1500 seconds)
                # This is slightly less than the typical 30-minute TTL
                cache.set(cache_key, access_token, timeout=1500)
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


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((urllib.error.HTTPError, urllib.error.URLError))
)
def provide_evidence(
    dispute_id: str,
    documents: List[DisputeDocument],
    response_text: str,
    evidence_type: str = DEFAULT_EVIDENCE_TYPE,
) -> bool:
    """
    Provide evidence to PayPal for a dispute.

    Uploads DisputeDocument PDFs as evidence and sends response text to PayPal.
    Makes a POST request to /v1/customer-disputes/{dispute_id}/provide-evidence.

    Args:
        dispute_id: The PayPal dispute ID
        documents: List of DisputeDocument instances to upload as evidence
        response_text: Text response/evidence to include with submission

    Returns:
        True if evidence was successfully submitted, False on failure.
        On success, updates the local Dispute status to EVIDENCE_SENT.
    """
    access_token = get_paypal_access_token()
    if not access_token:
        logger.error(f"Cannot provide evidence for dispute {dispute_id}: no access token")
        return False

    try:
        # Get the local Dispute record
        try:
            dispute = Dispute.objects.get(paypal_dispute_id=dispute_id)
        except Dispute.DoesNotExist:
            logger.error(f"Local Dispute record not found for PayPal dispute {dispute_id}")
            return False

        base_url = paypal_api_base()
        evidence_url = f"{base_url}/v1/customer/disputes/{dispute_id}/provide-evidence"

        # PayPal's provide-evidence is a MULTIPART upload: a JSON 'input' part
        # describing the evidences (each with an evidence_type), plus one part
        # per real file. (The previous base64-in-JSON form was rejected by
        # PayPal.) Exact field shape to be confirmed in sandbox.
        files = []
        for doc in documents:
            if not doc.file_path:
                logger.warning(f"Document {doc.id} has no file_path, skipping")
                continue
            try:
                doc.file_path.open('rb')
                content = doc.file_path.read()
                file_name = doc.file_path.name.split('/')[-1]
                files.append({
                    'name': file_name,
                    'filename': file_name,
                    'content': content,
                    'content_type': 'application/pdf',
                })
                logger.info(f"Attached document {doc.id} ({file_name}) to evidence upload")
            except Exception as e:
                logger.error(f"Error reading document {doc.id}: {e}")
                continue

        if not files:
            logger.error(f"No valid document files to submit for dispute {dispute_id}")
            return False

        input_json = {
            "evidences": [{
                "evidence_type": evidence_type,
                "notes": response_text or '',
                "document_ids": [f['filename'] for f in files],
            }]
        }
        request_data, content_type = _encode_multipart(input_json, files)
        request = urllib.request.Request(
            evidence_url,
            data=request_data,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': content_type,
            },
            method='POST'
        )

        # Use configurable timeout
        timeout = getattr(settings, 'PAYPAL_TIMEOUT', 30)

        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
        logger.info(f"Successfully submitted evidence for dispute {dispute_id}")

        # External call done — persist local state in its own short transaction.
        # (No network I/O inside the DB transaction: a slow/failed PayPal call no
        # longer holds a DB lock, and the writes here are all-or-nothing.)
        with transaction.atomic():
            for doc in documents:
                if doc.file_path:
                    doc.status = 'SENT'
                    doc.save(update_fields=['status'])
            dispute.status = 'EVIDENCE_SENT'
            dispute.save(update_fields=['status'])
            DisputeActivityLog.objects.create(
                dispute=dispute,
                action='EVIDENCE_SENT',
                details=f"Evidence submitted to PayPal. Documents: {[d.id for d in documents]}. Response length: {len(response_text)} chars",
            )

        logger.info(f"Updated Dispute #{dispute.id} status to EVIDENCE_SENT")
        return True

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error submitting evidence for dispute {dispute_id}: {e.code} - {error_body}")
        return False

    except urllib.error.URLError as e:
        logger.error(f"URL error submitting evidence for dispute {dispute_id}: {e.reason}")
        return False

    except Exception as e:
        logger.error(f"Unexpected error submitting evidence for dispute {dispute_id}: {e}")
        return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    retry=retry_if_exception_type((urllib.error.HTTPError, urllib.error.URLError))
)
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

        base_url = paypal_api_base()
        accept_url = f"{base_url}/v1/customer/disputes/{dispute_id}/accept-claim"

        # Build acceptance payload
        accept_payload = {}
        if note:
            accept_payload["note"] = note

        # Create request
        request_data = json.dumps(accept_payload).encode('utf-8')
        request = urllib.request.Request(
            accept_url,
            data=request_data,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            },
            method='POST'
        )

        # Use configurable timeout
        timeout = getattr(settings, 'PAYPAL_TIMEOUT', 30)

        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
        logger.info(f"Successfully accepted claim for dispute {dispute_id}")

        # External call done — persist local state in its own short transaction.
        with transaction.atomic():
            dispute.status = 'ACCEPTED'
            dispute.save(update_fields=['status'])
            DisputeActivityLog.objects.create(
                dispute=dispute,
                action='DISPUTE_RESOLVED',
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

        # Create request
        request_data = json.dumps(message_payload).encode('utf-8')
        request = urllib.request.Request(
            message_url,
            data=request_data,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            },
            method='POST'
        )

        # Use configurable timeout
        timeout = getattr(settings, 'PAYPAL_TIMEOUT', 30)

        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
        logger.info(f"Successfully sent message for dispute {dispute_id}")

        # External call done — log the activity in its own short transaction.
        with transaction.atomic():
            DisputeActivityLog.objects.create(
                dispute=dispute,
                action='NOTE_ADDED',
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
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode('utf-8'),
            headers={'Authorization': f'Bearer {access_token}',
                     'Content-Type': 'application/json'},
            method='POST')
        timeout = getattr(settings, 'PAYPAL_TIMEOUT', 30)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.loads(response.read().decode('utf-8'))
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
                if existing.status == 'RECEIVED':
                    existing.status = 'MATCHED'
                existing.save(update_fields=['claim', 'zd_ticket_id', 'status', 'updated_at'])
                DisputeActivityLog.objects.create(
                    dispute=existing, action='DISPUTE_MATCHED',
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

    dispute = Dispute.objects.create(
        paypal_dispute_id=dispute_id,
        paypal_case_id=details.get('case_id', '') or '',
        claim=claim,
        zd_ticket_id=(claim.zd_ticket_id if claim else ''),
        status='MATCHED' if claim else 'RECEIVED',
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
    DisputeActivityLog.objects.create(
        dispute=dispute, action='DISPUTE_CREATED',
        details=f"Ingested from PayPal ({details.get('dispute_life_cycle_stage', '?')} stage, "
                f"reason={reason or '?'}). {'Matched claim #%s' % claim.id if claim else 'No claim matched'}.")
    if claim:
        DisputeActivityLog.objects.create(
            dispute=dispute, action='DISPUTE_MATCHED',
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
        return dispute

    details = fetch_dispute_details(dispute_id)
    if not details:
        return dispute

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

    pp_status = (details.get('status') or '').upper()
    if pp_status == 'RESOLVED':
        outcome = (details.get('dispute_outcome') or {}).get('outcome_code', '') or ''
        won = 'SELLER' in outcome.upper()  # e.g. RESOLVED_SELLER_FAVOUR
        new_status = 'RESOLVED_WON' if won else 'RESOLVED_LOST'
        if dispute.status != new_status:
            dispute.status = new_status
            update_fields.append('status')
            DisputeActivityLog.objects.create(
                dispute=dispute, action='DISPUTE_RESOLVED',
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
