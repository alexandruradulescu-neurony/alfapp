"""
PayPal Disputes API Client for LORA.

Provides functions for interacting with PayPal's Customer Disputes API:
- OAuth2 token management with caching
- Fetching dispute details
- Providing evidence (documents and response text)
- Accepting claims (refunds)
- Sending messages to buyers

All API calls use the live PayPal API (production environment).
"""

import base64
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List

from django.core.cache import cache
from django.conf import settings
from django.db import transaction

from apps.payments.models import Dispute, DisputeDocument, DisputeActivityLog
from apps.config.models import SystemSettings

logger = logging.getLogger(__name__)


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
        base_url = "https://api.paypal.com"  # Always use live API
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
        base_url = "https://api.paypal.com"  # Always use live API
        dispute_url = f"{base_url}/v1/customer-disputes/{dispute_id}"

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


@transaction.atomic
def provide_evidence(
    dispute_id: str,
    documents: List[DisputeDocument],
    response_text: str
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

        base_url = "https://api.paypal.com"  # Always use live API
        evidence_url = f"{base_url}/v1/customer-disputes/{dispute_id}/provide-evidence"

        # Build evidence payload
        # PayPal expects evidence in a specific format with notes and supporting_files
        evidence_payload = {
            "notes": response_text,
            "supporting_files": []
        }

        # Process each document
        for doc in documents:
            if not doc.file_path:
                logger.warning(f"Document {doc.id} has no file_path, skipping")
                continue

            try:
                # Read the PDF file content
                doc.file_path.open('rb')
                file_content = doc.file_path.read()
                file_content_base64 = base64.b64encode(file_content).decode('utf-8')

                # Determine file name from the file path
                file_name = doc.file_path.name.split('/')[-1]

                # Add to supporting files
                evidence_payload["supporting_files"].append({
                    "file_name": file_name,
                    "file_type": "PDF",
                    "file_content": file_content_base64,
                    "notes": f"{doc.get_doc_type_display()} - Version {doc.version}"
                })

                logger.info(f"Added document {doc.id} ({file_name}) to evidence payload")

            except Exception as e:
                logger.error(f"Error processing document {doc.id}: {e}")
                continue

        if not evidence_payload["supporting_files"]:
            logger.warning(f"No valid documents to submit for dispute {dispute_id}")
            # Still allow submission with just notes if response_text is provided
            if not response_text:
                logger.error("No evidence (documents or response text) to submit")
                return False

        # Create request
        request_data = json.dumps(evidence_payload).encode('utf-8')
        request = urllib.request.Request(
            evidence_url,
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
            logger.info(f"Successfully submitted evidence for dispute {dispute_id}")

            # Update document statuses to SENT
            for doc in documents:
                if doc.file_path:
                    doc.status = 'SENT'
                    doc.save(update_fields=['status'])

            # Update Dispute status to EVIDENCE_SENT
            dispute.status = 'EVIDENCE_SENT'
            dispute.save(update_fields=['status'])

            # Log the activity
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


@transaction.atomic
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

        base_url = "https://api.paypal.com"  # Always use live API
        accept_url = f"{base_url}/v1/customer-disputes/{dispute_id}/accept-claim"

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

            # Update Dispute status to ACCEPTED
            dispute.status = 'ACCEPTED'
            dispute.save(update_fields=['status'])

            # Log the activity
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


@transaction.atomic
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

        base_url = "https://api.paypal.com"  # Always use live API
        message_url = f"{base_url}/v1/customer-disputes/{dispute_id}/message"

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

            # Log the activity
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
