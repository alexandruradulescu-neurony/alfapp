"""
Payment views for LORA.
Includes PayPal webhook handling and PDF proof of work generation.
"""

import json
import base64
import logging
import urllib.request
import urllib.error
from datetime import datetime
from django.utils import timezone
from typing import Dict, Any, Optional

from django.db import transaction
from django.conf import settings
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions

from apps.claims.models import Claim
from apps.config.models import SystemSettings
from apps.payments.utils import generate_proof_of_work_pdf
from apps.payments.models import (
    Dispute,
    DisputeActivityLog,
    ProcessedWebhookEvent,
)
from apps.payments.screenshot_service import capture_screenshots_manual
from apps.integrations.services import search_zendesk_ticket_for_dispute

logger = logging.getLogger(__name__)


def verify_paypal_webhook_signature(
    request_headers: Dict[str, str],
    request_body: bytes,
    webhook_id: str,
    client_id: str,
    secret: str
) -> bool:
    """
    Verify PayPal webhook signature using PayPal's verify-webhook-signature API.

    Args:
        request_headers: Django request headers (dict-like)
        request_body: Raw request body bytes
        webhook_id: PayPal webhook ID from SystemSettings
        client_id: PayPal client ID
        secret: PayPal secret

    Returns:
        True if signature is valid, False otherwise
    """
    try:
        # Extract required headers
        transmission_id = request_headers.get('PAYPAL-TRANSMISSION-ID', '')
        transmission_time = request_headers.get('PAYPAL-TRANSMISSION-TIME', '')
        transmission_sig = request_headers.get('PAYPAL-TRANSMISSION-SIG', '')
        cert_url = request_headers.get('PAYPAL-CERT-URL', '')
        auth_algo = request_headers.get('PAYPAL-AUTH-ALGO', '')

        # Validate required headers exist
        if not all([transmission_id, transmission_time, transmission_sig, cert_url, auth_algo]):
            logger.warning("Missing required PayPal webhook headers")
            return False

        # Build verification request
        base_url = "https://api.paypal.com"
        if settings.PAYPAL_MODE == 'sandbox':
            base_url = "https://api.sandbox.paypal.com"

        verify_url = f"{base_url}/v1/notifications/verify-webhook-signature"

        # Get OAuth2 access token (with caching to avoid repeated API calls)
        from django.core.cache import cache
        cache_key = f'paypal_access_token_{client_id}'

        # Try to get token from cache first (tokens typically valid for ~30 min)
        access_token = cache.get(cache_key)

        if not access_token:
            credentials = f"{client_id}:{secret}"
            encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')

            token_request = urllib.request.Request(
                f"{base_url}/v1/oauth2/token",
                data=b'grant_type=client_credentials',
                headers={
                    'Authorization': f'Basic {encoded_credentials}',
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                method='POST'
            )

            # Use configurable timeout
            timeout = getattr(settings, 'PAYPAL_TIMEOUT', 30)
            with urllib.request.urlopen(token_request, timeout=timeout) as response:
                token_data = json.loads(response.read().decode('utf-8'))
                access_token = token_data.get('access_token')

                # Cache token for 25 minutes (slightly less than typical 30 min TTL)
                if access_token:
                    cache.set(cache_key, access_token, timeout=1500)
                    logger.debug("PayPal access token cached for 25 minutes")

        if not access_token:
            logger.error("Failed to get PayPal access token")
            return False

        # Build verification payload
        verification_payload = {
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": transmission_time,
            "webhook_id": webhook_id,
            "webhook_event": json.loads(request_body.decode('utf-8')),
        }

        # Make verification request
        verify_request = urllib.request.Request(
            verify_url,
            data=json.dumps(verification_payload).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            },
            method='POST'
        )

        with urllib.request.urlopen(verify_request, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            verification_status = result.get('verification_status', '')

            if verification_status == 'SUCCESS':
                logger.info("PayPal webhook signature verified successfully")
                return True
            else:
                logger.warning(f"PayPal webhook signature verification failed: {verification_status}")
                return False

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8') if e.fp else ''
        logger.error(f"HTTP error verifying PayPal webhook: {e.code} - {error_body}")
        return False

    except urllib.error.URLError as e:
        logger.error(f"URL error verifying PayPal webhook: {e.reason}")
        return False

    except Exception as e:
        logger.error(f"Unexpected error verifying PayPal webhook: {e}")
        return False


class PayPalWebhookView(APIView):
    """
    PayPal webhook endpoint for handling payment events.

    POST /api/paypal/webhook/

    Handles:
    - CUSTOMER.DISPUTE.CREATED: Creates Dispute record, matches to Zendesk ticket
    - CUSTOMER.DISPUTE.UPDATED: Updates existing Dispute record
    - CUSTOMER.DISPUTE.RESOLVED: Marks Dispute as resolved

    Authentication: PayPal webhook signature verification
    """

    permission_classes = [permissions.AllowAny]  # PayPal webhooks are authenticated via signature

    def post(self, request, *args, **kwargs):
        """
        Handle incoming PayPal webhook events.
        Routes events to appropriate handlers based on event type.
        """
        try:
            # Get raw body for signature verification
            raw_body = request.body

            # Get headers (convert to dict with uppercase keys)
            headers = {key.upper(): value for key, value in request.headers.items()}

            # Get PayPal credentials from SystemSettings
            try:
                system_settings = SystemSettings.get_instance()
                paypal_client_id = system_settings.paypal_client_id
                paypal_secret = system_settings.paypal_secret
                paypal_webhook_id = system_settings.paypal_webhook_id
            except Exception as e:
                logger.error(f"Error loading PayPal credentials from SystemSettings: {e}")
                return Response(
                    {'error': 'PayPal configuration error'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Validate credentials are configured
            if not all([paypal_client_id, paypal_secret, paypal_webhook_id]):
                logger.warning("PayPal credentials not fully configured")
                return Response(
                    {'error': 'PayPal credentials not configured'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Verify webhook signature
            is_valid = verify_paypal_webhook_signature(
                request_headers=headers,
                request_body=raw_body,
                webhook_id=paypal_webhook_id,
                client_id=paypal_client_id,
                secret=paypal_secret,
            )

            if not is_valid:
                logger.warning("Invalid PayPal webhook signature")
                return Response(
                    {'error': 'Invalid signature'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Parse webhook event
            try:
                event_data = json.loads(raw_body.decode('utf-8'))
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in webhook payload: {e}")
                return Response(
                    {'error': 'Invalid JSON payload'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            event_id = event_data.get('id', '')
            event_type = event_data.get('event_type', '')
            resource = event_data.get('resource', {})

            # Check for duplicate event (idempotency)
            if event_id:
                if ProcessedWebhookEvent.is_already_processed(event_id):
                    logger.info(f"PayPal webhook event {event_id} already processed - skipping")
                    return Response({'status': 'duplicate', 'event_id': event_id})

            logger.info(f"Received PayPal webhook event: {event_type} (ID: {event_id})")

            # Route to appropriate handler based on event type
            if event_type == 'CUSTOMER.DISPUTE.CREATED':
                return self.handle_dispute_created(resource, event_id)
            elif event_type == 'CUSTOMER.DISPUTE.UPDATED':
                return self.handle_dispute_updated(resource, event_id)
            elif event_type == 'CUSTOMER.DISPUTE.RESOLVED':
                return self.handle_dispute_resolved(resource, event_id)

            # Mark other events as processed
            if event_id:
                ProcessedWebhookEvent.mark_as_processed(
                    event_id=event_id,
                    event_type=event_type,
                )

            # Other events can be logged but not processed
            logger.info(f"Webhook event {event_type} received but not processed")
            return Response({'status': 'received', 'event_type': event_type})

        except Exception as e:
            logger.error(f"Error processing PayPal webhook: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @transaction.atomic
    def handle_dispute_created(self, resource: Dict[str, Any], event_id: str = '') -> Response:
        """
        Handle CUSTOMER.DISPUTE.CREATED event.

        Steps:
        1. Extract full payload: dispute_id, reason, amount, buyer info, transaction_id, dates
        2. Create Dispute record with status RECEIVED, store raw payload
        3. Call search_zendesk_ticket_for_dispute() to find matching ticket
        4. If found: set zd_ticket_id, status -> MATCHED; link Claim if exists
        5. Log all actions to DisputeActivityLog
        6. Mark webhook as processed (ProcessedWebhookEvent)
        """
        try:
            # 1. Extract full payload
            dispute_id = resource.get('dispute_id', '') or resource.get('id', '')
            reason = resource.get('reason', '') or resource.get('dispute_reason', '')
            amount_data = resource.get('dispute_amount', {}) or resource.get('amount', {})
            dispute_amount = amount_data.get('value') if isinstance(amount_data, dict) else amount_data
            dispute_currency = amount_data.get('currency', '') if isinstance(amount_data, dict) else ''

            # Buyer information
            buyer_email = ''
            buyer_name = ''

            if 'disputed_transaction' in resource:
                transaction = resource['disputed_transaction']
                buyer_email = (
                    transaction.get('payer_email') or
                    transaction.get('email') or
                    transaction.get('buyer_email') or
                    ''
                )
                buyer_name = (
                    transaction.get('payer_name') or
                    transaction.get('name') or
                    transaction.get('buyer_name') or
                    ''
                )
            else:
                buyer_email = resource.get('payer_email', '') or resource.get('buyer_email', '')
                buyer_name = resource.get('payer_name', '') or resource.get('buyer_name', '')

            # Transaction information
            transaction_id = ''
            transaction_date = None

            if 'disputed_transaction' in resource:
                transaction = resource['disputed_transaction']
                transaction_id = transaction.get('transaction_id') or transaction.get('id', '')
                transaction_date_str = transaction.get('transaction_date') or transaction.get('create_time', '')
                if transaction_date_str:
                    try:
                        # Parse ISO 8601 date string
                        transaction_date = datetime.fromisoformat(transaction_date_str.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        logger.warning(f"Could not parse transaction date: {transaction_date_str}")

            # Additional dates
            seller_response_due = None
            seller_response_due_str = resource.get('seller_response_due_date', '')
            if seller_response_due_str:
                try:
                    seller_response_due = datetime.fromisoformat(seller_response_due_str.replace('Z', '+00:00'))
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse seller response due date: {seller_response_due_str}")

            # Validate required fields
            if not dispute_id:
                logger.warning("Dispute ID not found in webhook payload")
                if event_id:
                    ProcessedWebhookEvent.mark_as_failed(
                        event_id=event_id,
                        event_type='CUSTOMER.DISPUTE.CREATED',
                        error_message='Dispute ID not found in payload',
                        resource_type='dispute',
                    )
                return Response(
                    {'error': 'Dispute ID not found'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if not buyer_email:
                logger.warning("Buyer email not found in dispute payload")
                # Still create dispute but mark as processed with warning
                buyer_email = 'unknown@example.com'

            # 2. Create Dispute record with status RECEIVED
            # First check if dispute already exists (idempotency at dispute level)
            existing_dispute = Dispute.objects.filter(paypal_dispute_id=dispute_id).first()
            if existing_dispute:
                logger.info(f"Dispute {dispute_id} already exists, skipping creation")
                # Mark webhook as processed
                if event_id:
                    ProcessedWebhookEvent.mark_as_processed(
                        event_id=event_id,
                        event_type='CUSTOMER.DISPUTE.CREATED',
                        resource_type='dispute',
                        resource_id=dispute_id,
                    )
                return Response({
                    'status': 'duplicate_dispute',
                    'dispute_id': dispute_id,
                })

            # Find or create Claim for this dispute
            claim = None
            if buyer_email and buyer_email != 'unknown@example.com':
                claim = Claim.objects.filter(client_email=buyer_email.lower().strip()).first()

            if not claim:
                logger.warning(f"No claim found for buyer email: {buyer_email}")
                # Create a minimal claim reference or skip
                # For now, we'll create the dispute without linking to a claim
                # This can be manually linked later
                pass

            # Create the Dispute record
            dispute = Dispute.objects.create(
                paypal_dispute_id=dispute_id,
                paypal_case_id=resource.get('case_id', ''),
                claim=claim if claim else None,
                status='RECEIVED',
                dispute_reason=reason if reason in Dispute.VALID_REASONS else 'OTHER',
                dispute_amount=dispute_amount,
                dispute_currency=dispute_currency or 'USD',
                buyer_email=buyer_email.lower().strip() if buyer_email else '',
                buyer_name=buyer_name,
                transaction_id=transaction_id,
                transaction_date=transaction_date or timezone.now(),
                seller_response_due=seller_response_due,
                raw_webhook_payload=resource,
                notes=f"Dispute created via webhook event {event_id}",
            )

            logger.info(f"Created Dispute #{dispute.id} for PayPal dispute {dispute_id}")

            # Log the creation
            DisputeActivityLog.objects.create(
                dispute=dispute,
                action='DISPUTE_CREATED',
                details=f"PayPal dispute {dispute_id} created via webhook. Reason: {reason}",
            )

            # 3. Search for matching Zendesk ticket
            zd_ticket = search_zendesk_ticket_for_dispute(
                buyer_email=buyer_email,
                buyer_name=buyer_name,
                transaction_id=transaction_id,
                transaction_date=transaction_date.isoformat() if transaction_date else '',
            )

            if zd_ticket:
                # 4. If found: set zd_ticket_id, status -> MATCHED
                ticket_id = str(zd_ticket.get('id', ''))
                dispute.zd_ticket_id = ticket_id
                dispute.status = 'MATCHED'
                dispute.save()

                logger.info(f"Matched Dispute #{dispute.id} to Zendesk ticket {ticket_id}")

                # Log the match
                DisputeActivityLog.objects.create(
                    dispute=dispute,
                    action='DISPUTE_MATCHED',
                    details=f"Automatically matched to Zendesk ticket {ticket_id}",
                )
            else:
                logger.info(f"No matching Zendesk ticket found for Dispute #{dispute.id}")

            # 5. Mark webhook as processed
            if event_id:
                ProcessedWebhookEvent.mark_as_processed(
                    event_id=event_id,
                    event_type='CUSTOMER.DISPUTE.CREATED',
                    resource_type='dispute',
                    resource_id=dispute_id,
                )

            return Response({
                'status': 'processed',
                'dispute_id': dispute.id,
                'paypal_dispute_id': dispute_id,
                'claim_id': claim.id if claim else None,
                'zd_ticket_id': dispute.zd_ticket_id,
                'status_change': 'RECEIVED -> ' + dispute.status,
            })

        except Exception as e:
            logger.error(f"Error handling dispute created event: {e}")
            # Mark event as failed
            if event_id:
                ProcessedWebhookEvent.mark_as_failed(
                    event_id=event_id,
                    event_type='CUSTOMER.DISPUTE.CREATED',
                    error_message=str(e),
                    resource_type='dispute',
                    resource_id=resource.get('dispute_id', '') or resource.get('id', ''),
                )
            return Response(
                {'error': 'Error processing dispute'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @transaction.atomic
    def handle_dispute_updated(self, resource: Dict[str, Any], event_id: str = '') -> Response:
        """
        Handle CUSTOMER.DISPUTE.UPDATED event.

        Steps:
        1. Extract dispute_id from payload
        2. Find existing Dispute record
        3. Update dispute status and details from payload
        4. Log the update to DisputeActivityLog
        5. Mark webhook as processed
        """
        try:
            # 1. Extract dispute ID
            dispute_id = resource.get('dispute_id', '') or resource.get('id', '')

            if not dispute_id:
                logger.warning("Dispute ID not found in update webhook payload")
                if event_id:
                    ProcessedWebhookEvent.mark_as_failed(
                        event_id=event_id,
                        event_type='CUSTOMER.DISPUTE.UPDATED',
                        error_message='Dispute ID not found in payload',
                        resource_type='dispute',
                    )
                return Response(
                    {'error': 'Dispute ID not found'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 2. Find existing Dispute record
            dispute = Dispute.objects.filter(paypal_dispute_id=dispute_id).first()

            if not dispute:
                logger.warning(f"Dispute {dispute_id} not found for update event")
                # Mark as processed even if dispute not found (to avoid re-processing)
                if event_id:
                    ProcessedWebhookEvent.mark_as_processed(
                        event_id=event_id,
                        event_type='CUSTOMER.DISPUTE.UPDATED',
                        resource_type='dispute',
                        resource_id=dispute_id,
                    )
                return Response({
                    'status': 'dispute_not_found',
                    'paypal_dispute_id': dispute_id,
                })

            # 3. Update dispute details from payload
            old_status = dispute.status
            updates = []

            # Update status if provided
            new_status = resource.get('status', '')
            if new_status:
                # Map PayPal status to our internal status
                status_mapping = {
                    'OPEN': 'UNDER_REVIEW',
                    'PENDING_CUSTOMER_SERVICE_REVIEW': 'GATHERING_DATA',
                    'PENDING_MERCHANT_RESPONSE': 'GATHERING_DATA',
                    'UNDER_REVIEW': 'UNDER_REVIEW',
                    'RESOLVED': 'EVIDENCE_SENT',
                    'CLOSED': 'RESOLVED_WON',
                }
                mapped_status = status_mapping.get(new_status, dispute.status)

                # Validate mapped status is valid
                valid_statuses = [choice[0] for choice in Dispute.STATUS_CHOICES]
                if mapped_status in valid_statuses:
                    dispute.status = mapped_status
                    updates.append(f"status: {old_status} -> {mapped_status}")

            # Update reason if provided
            new_reason = resource.get('reason', '') or resource.get('dispute_reason', '')
            if new_reason and new_reason in Dispute.VALID_REASONS:
                dispute.dispute_reason = new_reason
                updates.append(f"reason: {new_reason}")

            # Update amount if provided
            amount_data = resource.get('dispute_amount', {}) or resource.get('amount', {})
            if amount_data:
                new_amount = amount_data.get('value') if isinstance(amount_data, dict) else amount_data
                if new_amount:
                    dispute.dispute_amount = new_amount
                    updates.append(f"amount: {new_amount}")

            # Update raw payload
            dispute.raw_webhook_payload = resource
            dispute.notes = f"{dispute.notes}\nUpdated via webhook event {event_id}" if dispute.notes else f"Updated via webhook event {event_id}"

            dispute.save()

            logger.info(f"Updated Dispute #{dispute.id}: {', '.join(updates) if updates else 'no changes'}")

            # 4. Log the update
            if updates:
                DisputeActivityLog.objects.create(
                    dispute=dispute,
                    action='STATUS_CHANGED',
                    details=f"Webhook update: {', '.join(updates)}",
                )

            # 5. Mark webhook as processed
            if event_id:
                ProcessedWebhookEvent.mark_as_processed(
                    event_id=event_id,
                    event_type='CUSTOMER.DISPUTE.UPDATED',
                    resource_type='dispute',
                    resource_id=dispute_id,
                )

            return Response({
                'status': 'processed',
                'dispute_id': dispute.id,
                'paypal_dispute_id': dispute_id,
                'updates': updates,
            })

        except Exception as e:
            logger.error(f"Error handling dispute updated event: {e}")
            # Mark event as failed
            if event_id:
                ProcessedWebhookEvent.mark_as_failed(
                    event_id=event_id,
                    event_type='CUSTOMER.DISPUTE.UPDATED',
                    error_message=str(e),
                    resource_type='dispute',
                    resource_id=resource.get('dispute_id', '') or resource.get('id', ''),
                )
            return Response(
                {'error': 'Error processing dispute update'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @transaction.atomic
    def handle_dispute_resolved(self, resource: Dict[str, Any], event_id: str = '') -> Response:
        """
        Handle CUSTOMER.DISPUTE.RESOLVED event.

        Steps:
        1. Extract dispute_id and resolution details from payload
        2. Find existing Dispute record
        3. Update dispute status based on resolution outcome
        4. Log the resolution to DisputeActivityLog
        5. Mark webhook as processed
        """
        try:
            # 1. Extract dispute ID and resolution details
            dispute_id = resource.get('dispute_id', '') or resource.get('id', '')
            resolution = resource.get('resolution', {}) or {}
            outcome = resolution.get('outcome', '') or resource.get('outcome', '')

            if not dispute_id:
                logger.warning("Dispute ID not found in resolved webhook payload")
                if event_id:
                    ProcessedWebhookEvent.mark_as_failed(
                        event_id=event_id,
                        event_type='CUSTOMER.DISPUTE.RESOLVED',
                        error_message='Dispute ID not found in payload',
                        resource_type='dispute',
                    )
                return Response(
                    {'error': 'Dispute ID not found'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # 2. Find existing Dispute record
            dispute = Dispute.objects.filter(paypal_dispute_id=dispute_id).first()

            if not dispute:
                logger.warning(f"Dispute {dispute_id} not found for resolved event")
                # Mark as processed even if dispute not found
                if event_id:
                    ProcessedWebhookEvent.mark_as_processed(
                        event_id=event_id,
                        event_type='CUSTOMER.DISPUTE.RESOLVED',
                        resource_type='dispute',
                        resource_id=dispute_id,
                    )
                return Response({
                    'status': 'dispute_not_found',
                    'paypal_dispute_id': dispute_id,
                })

            # 3. Update dispute status based on resolution outcome
            old_status = dispute.status

            # Map resolution outcome to our internal status
            if outcome:
                outcome_lower = outcome.lower()
                if outcome_lower in ['won', 'resolved_in_merchant_favor', 'merchant_won']:
                    dispute.status = 'RESOLVED_WON'
                elif outcome_lower in ['lost', 'resolved_in_customer_favor', 'customer_won']:
                    dispute.status = 'RESOLVED_LOST'
                elif outcome_lower in ['accepted', 'refunded', 'settled']:
                    dispute.status = 'ACCEPTED'
                else:
                    dispute.status = 'EVIDENCE_SENT'
            else:
                # Default to evidence sent if no outcome specified
                dispute.status = 'EVIDENCE_SENT'

            # Update notes with resolution details
            resolution_notes = []
            if outcome:
                resolution_notes.append(f"Outcome: {outcome}")

            refund_amount = resolution.get('refund_amount', {})
            if refund_amount:
                amount_value = refund_amount.get('value') if isinstance(refund_amount, dict) else refund_amount
                if amount_value:
                    resolution_notes.append(f"Refund amount: {amount_value}")

            if resolution_notes:
                dispute.notes = f"{dispute.notes}\nResolution: {'; '.join(resolution_notes)}" if dispute.notes else '; '.join(resolution_notes)

            dispute.raw_webhook_payload = resource
            dispute.save()

            logger.info(f"Resolved Dispute #{dispute.id}: {dispute.status}")

            # 4. Log the resolution
            DisputeActivityLog.objects.create(
                dispute=dispute,
                action='DISPUTE_RESOLVED',
                details=f"Resolution outcome: {outcome or 'N/A'}. Status changed from {old_status} to {dispute.status}",
            )

            # 5. Mark webhook as processed
            if event_id:
                ProcessedWebhookEvent.mark_as_processed(
                    event_id=event_id,
                    event_type='CUSTOMER.DISPUTE.RESOLVED',
                    resource_type='dispute',
                    resource_id=dispute_id,
                )

            return Response({
                'status': 'processed',
                'dispute_id': dispute.id,
                'paypal_dispute_id': dispute_id,
                'outcome': outcome,
                'final_status': dispute.status,
            })

        except Exception as e:
            logger.error(f"Error handling dispute resolved event: {e}")
            # Mark event as failed
            if event_id:
                ProcessedWebhookEvent.mark_as_failed(
                    event_id=event_id,
                    event_type='CUSTOMER.DISPUTE.RESOLVED',
                    error_message=str(e),
                    resource_type='dispute',
                    resource_id=resource.get('dispute_id', '') or resource.get('id', ''),
                )
            return Response(
                {'error': 'Error processing dispute resolution'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ProofOfWorkPDFView(APIView):
    """
    Generate and download proof of work PDF for a claim.

    GET /api/claims/{id}/proof-of-work/

    Authentication: Session/Basic auth
    Permission: MANAGER only
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, claim_id):
        """
        Generate and return PDF for download.
        """
        try:
            # Check if user is MANAGER
            if not hasattr(request.user, 'role') or request.user.role != 'MANAGER':
                return Response(
                    {'error': 'Only MANAGERS can download proof of work PDFs'},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Get claim
            claim = Claim.objects.filter(id=claim_id).first()
            if not claim:
                return Response(
                    {'error': 'Claim not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Generate PDF
            pdf_bytes = generate_proof_of_work_pdf(claim)

            if not pdf_bytes:
                return Response(
                    {'error': 'Failed to generate PDF'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            # Return as file download
            response = HttpResponse(
                pdf_bytes,
                content_type='application/pdf'
            )
            response['Content-Disposition'] = f'attachment; filename="proof_of_work_claim_{claim.id}.pdf"'
            response['Content-Length'] = len(pdf_bytes)

            logger.info(f"Proof of work PDF downloaded for claim #{claim.id} by {request.user}")
            return response

        except Exception as e:
            logger.error(f"Error generating proof of work PDF: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class DisputeScreenshotCaptureView(APIView):
    """
    Manually trigger Zendesk screenshot capture for a dispute.

    POST /api/disputes/{id}/capture-screenshot/

    Authentication: Session/Basic auth
    Permission: MANAGER only

    This endpoint triggers browser-based screenshot capture of the Zendesk ticket
    associated with the dispute. The screenshot is saved as a DisputeScreenshot
    record and the dispute status is progressed.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, dispute_id):
        """
        Trigger screenshot capture for a dispute.
        """
        try:
            # Check if user is MANAGER
            if not hasattr(request.user, 'role') or request.user.role != 'MANAGER':
                return Response(
                    {'error': 'Only MANAGERS can trigger screenshot capture'},
                    status=status.HTTP_403_FORBIDDEN
                )

            # Get dispute
            dispute = Dispute.objects.filter(id=dispute_id).first()
            if not dispute:
                return Response(
                    {'error': 'Dispute not found'},
                    status=status.HTTP_404_NOT_FOUND
                )

            # Validate dispute has Zendesk ticket
            if not dispute.zd_ticket_id:
                return Response(
                    {'error': 'Dispute has no Zendesk ticket ID (zd_ticket_id)'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            logger.info(f"Manual screenshot capture triggered for Dispute #{dispute_id} by {request.user}")

            # Call the screenshot service
            success, message = capture_screenshots_manual(dispute_id)

            if success:
                return Response({
                    'status': 'success',
                    'dispute_id': dispute_id,
                    'zd_ticket_id': dispute.zd_ticket_id,
                    'message': message,
                    'new_status': dispute.status,
                })
            else:
                return Response({
                    'status': 'failed',
                    'dispute_id': dispute_id,
                    'zd_ticket_id': dispute.zd_ticket_id,
                    'error': message,
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except Exception as e:
            logger.exception(f"Error in screenshot capture view: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
