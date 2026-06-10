"""
Zendesk integration views for LORA.
Provides API endpoints for Zendesk sidebar widget.
"""

import hmac
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Aggregate, F
from django.db.models.functions import TruncDate

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.payments.models import Dispute, Refund
from apps.payments.refund_service import RefundService
from apps.integrations.services import tag_zendesk_ticket_as_refunded, add_refund_comment_to_zendesk

logger = logging.getLogger(__name__)


def _safe_date(value):
    """Parse a Zendesk date string ('YYYY-MM-DD') into a date, or None on failure."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _safe_decimal(value):
    """Parse a numeric value into Decimal, or None on failure."""
    if value in (None, ''):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


class ZendeskSidebarAuth:
    """
    Custom authentication for Zendesk sidebar widget.
    Validates the Authorization header against the sidebar_secret_token.
    Uses constant-time comparison to prevent timing attacks.
    """

    @staticmethod
    def authenticate(request) -> bool:
        """
        Check if the Authorization header matches the sidebar secret token.
        Returns True if authenticated, False otherwise.
        Uses hmac.compare_digest for constant-time comparison.
        """
        auth_header = request.headers.get('Authorization', '')
        
        # Get the expected token from SystemSettings
        try:
            system_settings = SystemSettings.get_instance()
            expected_token = system_settings.sidebar_secret_token
        except Exception as e:
            logger.error(f"Error loading SystemSettings for sidebar auth: {e}")
            return False
        
        if not expected_token:
            logger.warning("Sidebar secret token not configured in SystemSettings")
            return False
        
        # Support both "Bearer <token>" and raw token formats
        if auth_header.startswith('Bearer '):
            provided_token = auth_header[7:]  # Remove "Bearer " prefix
        else:
            provided_token = auth_header
        
        # Use constant-time comparison to prevent timing attacks
        return hmac.compare_digest(
            provided_token.encode('utf-8'),
            expected_token.encode('utf-8')
        )


class ZendeskSidebarView(APIView):
    """
    Zendesk sidebar widget endpoint.

    GET /api/zd/info/?email=<customer_email>[&ticket_id=<zd_ticket_id>]

    Authentication: Authorization header must match sidebar_secret_token from SystemSettings.

    Parameters:
    - email: Customer email address (optional if ticket_id provided)
    - ticket_id: Zendesk ticket ID (optional if email provided)

    If ticket_id provided: fetches claim by zd_ticket_id, extracts requester email
    If email provided: uses email directly to lookup claim
    Both can be provided for flexibility

    Returns enriched payload with:
    - claim: status and details if linked (existing functionality)
    - emails: {
        total: count of emails,
        unresolved: count (action_required=True AND NOT auto_resolved),
        latest_category: most recent email's category,
        category_breakdown: {OBJECT_FOUND: N, OBJECT_NOT_FOUND: N, ...}
      }
    - disputes: {
        total: count of disputes,
        active: [{id, status, amount, currency, seller_response_due}] (up to 5 most recent)
      }
    - submissions_tracking: {
        total: count of SUBMISSION_CONFIRMATION emails,
        responses_received: count of GENERAL_CORRESPONDENCE emails
      }

    Returns 404 if no claim found for the email/ticket_id.
    Returns 403 if authentication fails.
    """

    permission_classes = [AllowAny]  # Custom auth via sidebar secret token

    def get(self, request, *args, **kwargs):
        # Get parameters from query params
        customer_email = request.query_params.get('email', '').strip().lower()
        ticket_id = request.query_params.get('ticket_id', '').strip()

        logger.info(f"Sidebar data request - email: {customer_email or 'N/A'}, ticket_id: {ticket_id or 'N/A'}")

        # Validate that at least one parameter is provided
        if not customer_email and not ticket_id:
            return Response(
                {'error': 'Either email or ticket_id parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Authenticate using sidebar secret token
        if not ZendeskSidebarAuth.authenticate(request):
            # Rate limit failed auth attempts by IP
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)  # 5 min window
            
            logger.warning(f"Failed sidebar auth attempt for email: {customer_email or 'N/A'}, ticket_id: {ticket_id or 'N/A'}, IP: {ip}, attempt: {failed_attempts + 1}")
            
            if failed_attempts >= 5:
                return Response(
                    {'error': 'Too many failed attempts. Please try again later.'},
                    status=status.HTTP_429_TOO_MANY_REQUESTS
                )
            
            return Response(
                {'error': 'Unauthorized'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Lookup claim
        claim = None
        try:
            if ticket_id:
                # First try to lookup by ticket_id
                claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()
                if claim:
                    # Extract requester email from the claim
                    customer_email = claim.client_email
                    logger.info(f"Found claim #{claim.id} via ticket_id: {ticket_id}")
            
            # If no claim found yet, try lookup by email
            if not claim and customer_email:
                claim = Claim.objects.filter(client_email=customer_email).first()
                if claim:
                    logger.info(f"Found claim #{claim.id} via email: {customer_email}")

            if not claim:
                logger.info(f"No claim found for email: {customer_email}, ticket_id: {ticket_id}")
                return Response(
                    {'error': 'No claim found for this email or ticket_id'},
                    status=status.HTTP_404_NOT_FOUND
                )

        except Exception as e:
            logger.error(f"Error fetching claim for email: {customer_email}, ticket_id: {ticket_id}: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        try:
            # Build enriched response data
            response_data = self._build_enriched_sidebar_data(claim, customer_email)
            
            logger.info(f"Sidebar data returned for claim #{claim.id}, email: {customer_email}")
            return Response(response_data)

        except Exception as e:
            logger.error(f"Error building sidebar data for claim #{claim.id}, email: {customer_email}: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _build_enriched_sidebar_data(self, claim, customer_email):
        """
        Build enriched sidebar data with claim, emails, disputes, and submissions tracking.
        Uses optimized queries with .aggregate() for counts and .values() for breakdowns.
        """
        # Base claim data (existing functionality)
        base_data = {
            'found': True,
            'claim_id': claim.id,
            'claim_status': claim.status,
            'zd_ticket_id': claim.zd_ticket_id or '',
            'created_at': claim.created_at.isoformat() if claim.created_at else None,
            'flight_details': claim.flight_details or '',
        }

        # Enrich with emails data
        emails_data = self._get_emails_data(claim)
        
        # Enrich with disputes data
        disputes_data = self._get_disputes_data(claim)
        
        # Enrich with submissions tracking
        submissions_data = self._get_submissions_tracking(claim)

        # Combine all data
        response_data = {
            **base_data,
            'emails_processed': emails_data['total'],  # Backward compatibility
            'emails': emails_data,
            'disputes': disputes_data,
            'submissions_tracking': submissions_data,
        }

        return response_data

    def _get_emails_data(self, claim):
        """
        Get enriched email statistics for a claim.
        Uses optimized queries with .aggregate() for counts.
        """
        # Total count
        total_count = EmailLog.objects.filter(claim=claim).count()
        
        # Unresolved count: action_required=True AND auto_resolved=False
        unresolved_count = EmailLog.objects.filter(
            claim=claim,
            action_required=True,
            auto_resolved=False
        ).count()
        
        # Latest email category (most recent by received_at)
        latest_email = EmailLog.objects.filter(claim=claim).order_by('-received_at').first()
        latest_category = latest_email.category if latest_email else None
        
        # Category breakdown using .values() and .annotate()
        category_breakdown = {}
        category_counts = EmailLog.objects.filter(claim=claim).values('category').annotate(
            count=Count('id')
        ).order_by('-count')
        
        for item in category_counts:
            category_breakdown[item['category']] = item['count']
        
        return {
            'total': total_count,
            'unresolved': unresolved_count,
            'latest_category': latest_category,
            'category_breakdown': category_breakdown,
        }

    def _get_disputes_data(self, claim):
        """
        Get dispute statistics for a claim.
        Returns total count and up to 5 most recent active disputes.
        """
        # Total count
        total_count = Dispute.objects.filter(claim=claim).count()
        
        # Active disputes: any dispute that is not in a resolved state
        # Active statuses: RECEIVED, MATCHED, GATHERING_DATA, DOCUMENTS_READY, UNDER_REVIEW, EVIDENCE_SENT
        active_statuses = ['RECEIVED', 'MATCHED', 'GATHERING_DATA', 'DOCUMENTS_READY', 'UNDER_REVIEW', 'EVIDENCE_SENT']
        
        active_disputes_qs = Dispute.objects.filter(
            claim=claim,
            status__in=active_statuses
        ).order_by('-created_at')[:5]  # Limit to 5 most recent
        
        active_disputes = [
            {
                'id': dispute.id,
                'status': dispute.status,
                'amount': str(dispute.dispute_amount) if dispute.dispute_amount else None,
                'currency': dispute.dispute_currency,
                'seller_response_due': dispute.seller_response_due.isoformat() if dispute.seller_response_due else None,
            }
            for dispute in active_disputes_qs
        ]
        
        return {
            'total': total_count,
            'active': active_disputes,
        }

    def _get_submissions_tracking(self, claim):
        """
        Track submission-related emails for a claim.
        - total: count of SUBMISSION_CONFIRMATION emails
        - responses_received: count of GENERAL_CORRESPONDENCE emails
        """
        # Count SUBMISSION_CONFIRMATION emails
        submission_confirmations = EmailLog.objects.filter(
            claim=claim,
            category='SUBMISSION_CONFIRMATION'
        ).count()
        
        # Count GENERAL_CORRESPONDENCE emails (responses)
        general_correspondence = EmailLog.objects.filter(
            claim=claim,
            category='GENERAL_CORRESPONDENCE'
        ).count()
        
        return {
            'total': submission_confirmations,
            'responses_received': general_correspondence,
        }


class ZendeskBriefingView(APIView):
    """POST /api/integrations/zd/briefing/
    Body: {ticket_id, requester_email, subject, description, comments[]}
    Returns: {summary, next_steps[], facts{}} — AI briefing + LORA facts.
    Auth: ZendeskSidebarAuth (sidebar_secret_token)."""

    permission_classes = [AllowAny]

    BRIEFING_PROMPT = (
        "You are briefing a lost-item recovery agent who is about to handle a "
        "ticket. Using ONLY the provided ticket content and claim facts, write a "
        "2-3 sentence summary of where this claim stands, then list up to 4 "
        "concrete next steps the agent should take. Respond as JSON: "
        '{"summary": "...", "next_steps": ["..."]}.'
    )

    def post(self, request):
        if not ZendeskSidebarAuth.authenticate(request):
            ip = request.META.get('REMOTE_ADDR', '')
            cache_key = f'sidebar_auth_fail_{ip}'
            failed_attempts = cache.get(cache_key, 0)
            cache.set(cache_key, failed_attempts + 1, 300)
            logger.warning(f"Failed briefing auth attempt, IP: {ip}, attempt: {failed_attempts + 1}")
            if failed_attempts >= 5:
                return Response({'error': 'Too many failed attempts. Please try again later.'},
                                status=status.HTTP_429_TOO_MANY_REQUESTS)
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        from apps.ai.client import AIClient
        from apps.ai.schemas import BriefingSummary
        from apps.ai.exceptions import AIResponseValidationError
        from apps.claims.models import Claim
        from apps.integrations.services import build_claim_facts

        data = request.data
        ticket_id = str(data.get('ticket_id', '')).strip()
        logger.info(f"Briefing request for ticket_id: {ticket_id or 'N/A'}")
        subject = str(data.get('subject', ''))
        description = str(data.get('description', ''))
        comments = data.get('comments') or []
        if not isinstance(comments, list):
            comments = [str(comments)]
        comments = [str(c)[:1000] for c in comments[:10]]

        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None
        facts = build_claim_facts(claim) if claim else {}

        trusted = {'claim_facts': str(facts)} if facts else None
        untrusted = {'ticket_subject': subject[:200], 'ticket_description': description[:2000]}
        if comments:
            untrusted['zendesk_comment'] = comments

        try:
            result = AIClient.complete(
                system_prompt=self.BRIEFING_PROMPT,
                trusted=trusted,
                untrusted=untrusted,
                known_pii={'aliases': []},
                response_schema=BriefingSummary,
                call_site='zendesk_briefing',
                temperature=0.4,
                max_tokens=500,
            )
        except AIResponseValidationError as e:
            logger.warning(f"Briefing AI validation failed for ticket {ticket_id}: {e}")
            return Response(
                {'summary': 'Briefing unavailable right now. Please use the Chat tab or retry.',
                 'next_steps': [], 'facts': facts},
                status=status.HTTP_200_OK,
            )

        return Response(
            {'summary': result.summary, 'next_steps': result.next_steps, 'facts': facts},
            status=status.HTTP_200_OK,
        )


class ZendeskTicketSyncView(APIView):
    """
    Endpoint to sync a claim with Zendesk.
    Creates a Zendesk ticket if the claim doesn't have one.
    
    POST /api/zd/sync/
    Body: {"claim_id": <id>}
    
    Authentication: Sidebar secret token
    """

    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        # Authenticate
        if not ZendeskSidebarAuth.authenticate(request):
            return Response(
                {'error': 'Unauthorized'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        claim_id = request.data.get('claim_id')
        
        if not claim_id:
            return Response(
                {'error': 'claim_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from apps.integrations.services import create_zendesk_ticket
            
            claim = Claim.objects.filter(id=claim_id).first()
            if not claim:
                return Response(
                    {'error': 'Claim not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Check if ticket already exists
            if claim.zd_ticket_id:
                return Response({
                    'message': 'Ticket already exists',
                    'zd_ticket_id': claim.zd_ticket_id,
                })
            
            # Create Zendesk ticket
            ticket_data = create_zendesk_ticket(
                subject=f"Lost Object Claim #{claim.id} - {claim.client_email}",
                comment_body=f"Claim details:\n\nFlight: {claim.flight_details or 'Not provided'}\nStatus: {claim.status}",
                requester_email=claim.client_email,
                tags=['lora', 'lost-object', f'claim-{claim.id}'],
            )
            
            if ticket_data:
                # Update claim with ticket ID
                claim.zd_ticket_id = str(ticket_data['id'])
                claim.save()
                
                return Response({
                    'message': 'Ticket created successfully',
                    'zd_ticket_id': ticket_data['id'],
                    'ticket_url': ticket_data.get('url', ''),
                })
            else:
                return Response(
                    {'error': 'Failed to create Zendesk ticket'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            logger.error(f"Error syncing claim {claim_id} to Zendesk: {e}")
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RefundWebhookView(APIView):
    """
    Webhook endpoint for receiving refund notifications from Zendesk/WordPress.
    
    Expects POST request with JSON payload:
    {
        "event": "refund_processed",
        "claim_number": "123",
        "refund_id": "WC-456",
        "refund_amount": "50.00",
        "currency": "USD",
        "reason": "Customer request",
        "order_id": "789",
        "zd_ticket_id": "12345"
    }
    
    Implements idempotency via Refund.paypal_refund_id unique constraint.
    """
    permission_classes = [AllowAny]  # TODO: Add webhook signature verification
    
    def post(self, request):
        """
        Process refund webhook from WordPress/Zendesk.
        """
        try:
            data = request.data
            
            # Validate required fields
            required_fields = ['claim_number', 'refund_id', 'refund_amount']
            for field in required_fields:
                if field not in data:
                    logger.warning(f"Missing required field: {field}")
                    return Response(
                        {'error': f'Missing required field: {field}'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # Verify webhook signature (TODO: Implement based on WordPress setup)
            # For now, verify optional secret token
            webhook_secret = request.headers.get('X-Webhook-Secret', '')
            if webhook_secret:
                system_settings = SystemSettings.get_instance()
                expected_secret = system_settings.sidebar_secret_token
                if not hmac.compare_digest(webhook_secret, expected_secret):
                    logger.warning("Invalid webhook secret")
                    return Response(
                        {'error': 'Invalid webhook secret'},
                        status=status.HTTP_401_UNAUTHORIZED
                    )
            
            # Process refund
            service = RefundService()
            result = service.process_woocommerce_refund(
                claim_number=str(data['claim_number']),
                refund_amount=data['refund_amount'],
                refund_id=str(data['refund_id']),
                order_id=str(data.get('order_id', '')),
                reason=data.get('reason', ''),
            )
            
            if result['success']:
                # Tag Zendesk ticket if provided
                zd_ticket_id = data.get('zd_ticket_id')
                if zd_ticket_id:
                    tag_zendesk_ticket_as_refunded(zd_ticket_id)
                    add_refund_comment_to_zendesk(
                        zd_ticket_id=zd_ticket_id,
                        refund_amount=f"{data['currency']} {data['refund_amount']}",
                        refund_id=str(data['refund_id']),
                        reason=data.get('reason', ''),
                    )
                
                return Response({
                    'message': 'Refund processed successfully',
                    'refund_id': result['refund'].paypal_refund_id,
                })
            else:
                logger.error(f"Refund processing failed: {result.get('error')}")
                return Response(
                    {'error': result.get('error', 'Processing failed')},
                    status=status.HTTP_400_BAD_REQUEST
                )
                
        except Exception as e:
            logger.error(f"Error processing refund webhook: {e}", exc_info=True)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ZendeskStatusWebhookView(APIView):
    """
    Webhook endpoint for receiving Zendesk ticket status changes.
    
    Expects POST request with JSON payload:
    {
        "ticket_id": "12345",
        "status": "refund_requested",
        "claim_id": "678"
    }
    
    When Zendesk status changes to "refund_requested", this updates the Claim status.
    """
    permission_classes = [AllowAny]  # TODO: Add webhook signature verification
    
    def post(self, request):
        """
        Process Zendesk status change webhook.
        """
        try:
            data = request.data
            
            # Validate required fields
            ticket_id = data.get('ticket_id')
            new_status = data.get('status')
            claim_id = data.get('claim_id')
            
            if not all([ticket_id, new_status, claim_id]):
                logger.warning(f"Missing required fields in Zendesk webhook: {data}")
                return Response(
                    {'error': 'Missing required fields'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Verify webhook secret
            webhook_secret = request.headers.get('X-Webhook-Secret', '')
            if webhook_secret:
                system_settings = SystemSettings.get_instance()
                expected_secret = system_settings.sidebar_secret_token
                if not hmac.compare_digest(webhook_secret, expected_secret):
                    logger.warning("Invalid webhook secret for Zendesk status change")
                    return Response(
                        {'error': 'Invalid webhook secret'},
                        status=status.HTTP_401_UNAUTHORIZED
                    )
            
            # Find claim by Zendesk ticket ID
            from apps.claims.models import Claim
            try:
                claim = Claim.objects.get(zd_ticket_id=ticket_id)
            except Claim.DoesNotExist:
                # Try by claim_id if provided
                try:
                    claim = Claim.objects.get(id=claim_id)
                except Claim.DoesNotExist:
                    logger.warning(f"Claim not found for ticket {ticket_id}")
                    return Response(
                        {'error': 'Claim not found'},
                        status=status.HTTP_404_NOT_FOUND
                    )
            
            # Update claim status based on Zendesk status
            if new_status == 'refund_requested':
                if claim.status not in ['REFUND_REQUESTED', 'REFUNDED', 'PARTIALLY_REFUNDED']:
                    claim.status = 'REFUND_REQUESTED'
                    claim.save()
                    logger.info(f"Claim #{claim.id} status updated to REFUND_REQUESTED from Zendesk")
                    
                    # Create a refund request record
                    from apps.payments.models import Refund
                    Refund.objects.get_or_create(
                        claim=claim,
                        defaults={
                            'status': 'REQUESTED',
                            'refund_type': 'FULL',
                            'external_source': 'LORA',
                            'reason': 'Refund requested via Zendesk',
                            'metadata': {'zendesk_ticket_id': ticket_id},
                        }
                    )
            
            return Response({
                'message': 'Status updated successfully',
                'claim_id': claim.id,
                'new_status': claim.status,
            })
            
        except Exception as e:
            logger.error(f"Error processing Zendesk status webhook: {e}", exc_info=True)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ZendeskClaimWebhookView(APIView):
    """
    Webhook endpoint for creating claims from Zendesk tickets.

    Triggered when Zendesk ticket status changes to 'investigation_initiated'.
    Uses custom status ID: 11688538967068

    Expects POST request with JSON payload (zen:event-type:ticket.custom_status_changed):
    {
        "event": {
            "current": "11688538967068",
            "previous": "8475923145214"
        },
        "detail": {
            "id": "41960",
            "subject": "Lost Item - ALF1234567",
            "custom_status": "11688538967068",
            "status": "OPEN",
            "requester_id": "8645878250110"
        }
    }

    Process:
    1. Validate webhook secret
    2. Check if claim already exists (by zd_ticket_id) - skip if exists
    3. Fetch full ticket data from Zendesk API
    4. Parse ALF claim ID from subject
    5. Call LLM to extract claim data
    6. Create Claim entity
    7. Set llm_extraction_failed flag if LLM failed

    Idempotency: Duplicate webhooks for same ticket are skipped.
    """

    # Zendesk custom status ID for "Investigation Initiated"
    INVESTIGATION_STATUS_ID = '11688538967068'
    permission_classes = [AllowAny]  # Webhook secret verification

    def post(self, request):
        """
        Process Zendesk claim creation webhook.
        """
        try:
            data = request.data

            # DEBUG: Log full webhook payload for troubleshooting
            import json
            logger.info(f"=== ZENDESK WEBHOOK PAYLOAD ===")
            logger.info(f"Headers: {dict(request.headers)}")
            logger.info(f"Body: {json.dumps(data, indent=2, default=str)}")
            logger.info(f"=== END PAYLOAD ===")

            # Extract data from Zendesk webhook payload structure
            # event.current contains the custom status ID
            event_data = data.get('event', {})
            detail_data = data.get('detail', {})

            # Get custom status from event.current or detail.custom_status
            custom_status = event_data.get('current') or detail_data.get('custom_status', '')

            # Get ticket details from detail object
            ticket_id = detail_data.get('id') or data.get('ticket_id')
            subject = detail_data.get('subject', '')
            requester_id = detail_data.get('requester_id')

            if not ticket_id:
                logger.warning(f"Missing ticket_id in webhook payload: {data}")
                return Response(
                    {'error': 'Missing required field: ticket_id'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Verify webhook secret
            webhook_secret = request.headers.get('X-Webhook-Secret', '')
            if webhook_secret:
                system_settings = SystemSettings.get_instance()
                expected_secret = system_settings.sidebar_secret_token
                if not hmac.compare_digest(webhook_secret, expected_secret):
                    logger.warning("Invalid webhook secret for claim creation")
                    return Response(
                        {'error': 'Invalid webhook secret'},
                        status=status.HTTP_401_UNAUTHORIZED
                    )

            # Validate status - must be "Investigation Initiated" (custom status ID)
            if str(custom_status) != self.INVESTIGATION_STATUS_ID:
                logger.info(f"Ignoring webhook for ticket {ticket_id}: custom_status '{custom_status}' is not investigation initiated")
                return Response({
                    'message': 'Ignored: status is not investigation initiated',
                    'custom_status': custom_status,
                }, status=status.HTTP_200_OK)

            # Check if claim already exists (idempotency)
            from apps.claims.models import Claim
            existing_claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()
            
            if existing_claim:
                logger.info(f"Claim already exists for Zendesk ticket {ticket_id} (Claim #{existing_claim.id})")
                return Response({
                    'message': 'Claim already exists',
                    'claim_id': existing_claim.id,
                    'alf_claim_id': existing_claim.alf_claim_id,
                }, status=status.HTTP_200_OK)
            
            # Fetch full ticket data from Zendesk API
            from apps.integrations.services import (
                fetch_zendesk_ticket,
                fetch_zendesk_comments,
                analyze_zendesk_ticket_for_claim,
                parse_alf_claim_id_from_subject,
            )
            
            ticket_data = fetch_zendesk_ticket(ticket_id)
            if not ticket_data:
                logger.error(f"Failed to fetch Zendesk ticket {ticket_id}")
                return Response(
                    {'error': 'Failed to fetch Zendesk ticket'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Fetch comments for LLM analysis
            comments = fetch_zendesk_comments(ticket_id)
            ticket_data['comments'] = comments
            
            # Parse ALF claim ID from subject
            alf_claim_id = parse_alf_claim_id_from_subject(subject)
            
            if not alf_claim_id:
                logger.warning(f"No ALF claim ID found in subject: {subject}")
                # Generate a placeholder if not found (should not happen)
                alf_claim_id = f"ALF{ticket_id.zfill(7)}"

            # DEBUG: Log ticket data before LLM extraction
            logger.info(f"=== TICKET DATA FOR LLM ===")
            logger.info(f"Ticket ID: {ticket_id}")
            logger.info(f"Subject: {subject}")
            logger.info(f"Ticket data keys: {list(ticket_data.keys())}")
            logger.info(f"Requester ID: {ticket_data.get('requester_id')}")
            logger.info(f"Comments count: {len(ticket_data.get('comments', []))}")
            logger.info(f"=== END TICKET DATA ===")

            # Call LLM to extract claim data
            try:
                extracted_data = analyze_zendesk_ticket_for_claim(ticket_data)
                logger.info(f"=== LLM EXTRACTION RESULT ===")
                logger.info(f"Extracted data: {extracted_data}")
                logger.info(f"=== END EXTRACTION RESULT ===")
            except Exception as e:
                logger.error(f"LLM extraction failed: {e}", exc_info=True)
                # Use empty data - will trigger fallback to requester email
                extracted_data = {
                    'client_email': '',
                    'flight_details': '',
                    'object_description': '',
                    'phone': '',
                    'alternate_email': '',
                }

            # Prefer the structured "Claim #" Zendesk field over the subject-parsed
            # ID. The field is authoritative; the subject line is the fallback
            # (already resolved into alf_claim_id above). Only override when the
            # field holds a parseable ALF id, so a blank or malformed field value
            # falls back to the subject-derived id rather than corrupting it.
            claim_number_field = extracted_data.get('claim_number', '')
            if claim_number_field:
                parsed_from_field = parse_alf_claim_id_from_subject(claim_number_field)
                if parsed_from_field:
                    alf_claim_id = parsed_from_field
                    logger.info(f"Using ALF claim ID from Zendesk 'Claim #' field: {alf_claim_id}")

            # Determine if LLM extraction failed
            llm_failed = not extracted_data.get('client_email') and not extracted_data.get('flight_details')
            logger.info(f"LLM extraction failed flag: {llm_failed}")

            # Use requester email as fallback if LLM didn't extract email
            client_email = extracted_data.get('client_email', '')
            if not client_email:
                # Try to get email from webhook requester object (if present)
                requester_email = data.get('requester', {}).get('email', '')
                if requester_email:
                    client_email = requester_email
                    logger.info(f"Using requester email from webhook as fallback: {client_email}")
                else:
                    # Fetch user email from Zendesk API using requester_id
                    requester_id = detail_data.get('requester_id') or ticket_data.get('requester_id')
                    if requester_id:
                        from apps.integrations.services import fetch_zendesk_user
                        user_data = fetch_zendesk_user(requester_id)
                        if user_data:
                            client_email = user_data.get('email', '')
                            logger.info(f"Using requester email from Zendesk API: {client_email}")

            # If every email-resolution path failed, the claim cannot be routed by
            # downstream automation. Force the manual-review flag and warn loudly
            # so operators see it in the queue rather than letting it sit silently.
            if not client_email:
                llm_failed = True
                logger.warning(
                    f"Could not resolve client_email for Zendesk ticket {ticket_id} "
                    f"via any path (LLM extraction, webhook requester, Zendesk user API). "
                    f"Claim will be flagged for manual review."
                )

            # Generate AI summary from extracted data (for claim detail page display)
            ai_summary_parts = []
            if extracted_data.get('client_name'):
                ai_summary_parts.append(f"Client: {extracted_data['client_name']}.")
            if extracted_data.get('flight_details'):
                ai_summary_parts.append(f"Flight: {extracted_data['flight_details']}.")
            if extracted_data.get('object_description'):
                ai_summary_parts.append(f"Lost item: {extracted_data['object_description']}.")
            if extracted_data.get('phone'):
                ai_summary_parts.append(f"Phone: {extracted_data['phone']}.")
            if extracted_data.get('alternate_email'):
                ai_summary_parts.append(f"Alternate email: {extracted_data['alternate_email']}.")
            
            ai_summary = ' '.join(ai_summary_parts) if ai_summary_parts else ''

            # Create Claim. The early existence check above is a cheap optimization
            # for the common case; concurrent webhooks can still race past it. The
            # DB-level unique constraint on zd_ticket_id catches that race here.
            # The atomic() savepoint isolates the create so that an IntegrityError
            # only rolls back the failed insert — not any surrounding transaction —
            # leaving us free to query for the existing Claim afterward.
            try:
                with transaction.atomic():
                    claim = Claim.objects.create(
                        alf_claim_id=alf_claim_id,
                        zd_ticket_id=ticket_id,
                        client_email=client_email,
                        client_name=extracted_data.get('client_name', ''),
                        flight_details=extracted_data.get('flight_details', ''),
                        object_description=extracted_data.get('object_description', ''),
                        phone=extracted_data.get('phone', ''),
                        alternate_email=extracted_data.get('alternate_email', ''),
                        # Extended structured fields (2026-06-10). deadline_date and
                        # price_paid are coerced to their DB types; bad/empty values
                        # become None rather than raising.
                        billing_address=extracted_data.get('billing_address', ''),
                        shipping_address=extracted_data.get('shipping_address', ''),
                        incident_details=extracted_data.get('incident_details', ''),
                        lost_location=extracted_data.get('lost_location', ''),
                        deadline_date=_safe_date(extracted_data.get('deadline_date', '')),
                        deadline_time=extracted_data.get('deadline_time', ''),
                        deadline_timezone=extracted_data.get('deadline_timezone', ''),
                        price_paid=_safe_decimal(extracted_data.get('price_paid', '')),
                        payment_method=extracted_data.get('payment_method', ''),
                        payment_status=extracted_data.get('payment_status', ''),
                        woocommerce_id=extracted_data.get('woocommerce_id', ''),
                        tracking_info=extracted_data.get('tracking_info', ''),
                        status='Received',
                        llm_extraction_failed=llm_failed,
                        ai_summary=ai_summary,
                    )
            except IntegrityError:
                # Another concurrent webhook created the Claim between our early
                # check and our create. Look up the winner and return its info.
                existing = Claim.objects.filter(zd_ticket_id=ticket_id).first()
                if not existing:
                    # IntegrityError for some other reason (e.g., alf_claim_id collision
                    # with an unrelated claim). Let the outer handler return 500.
                    raise
                logger.info(
                    f"Race with concurrent webhook for ticket {ticket_id}; "
                    f"existing Claim #{existing.id} ({existing.alf_claim_id}) wins."
                )
                return Response({
                    'message': 'Claim already exists',
                    'claim_id': existing.id,
                    'alf_claim_id': existing.alf_claim_id,
                }, status=status.HTTP_200_OK)

            logger.info(
                f"Created Claim #{claim.id} ({alf_claim_id}) from Zendesk ticket {ticket_id}. "
                f"LLM failed: {llm_failed}"
            )

            return Response({
                'message': 'Claim created successfully',
                'claim_id': claim.id,
                'alf_claim_id': claim.alf_claim_id,
                'zd_ticket_id': claim.zd_ticket_id,
                'llm_extraction_failed': claim.llm_extraction_failed,
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Error processing Zendesk claim webhook: {e}", exc_info=True)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
