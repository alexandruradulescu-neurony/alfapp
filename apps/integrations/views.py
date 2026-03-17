"""
Zendesk integration views for LORA.
Provides API endpoints for Zendesk sidebar widget.
"""

import hmac
import logging

from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from django.conf import settings
from django.db.models import Count, Q, Aggregate, F
from django.db.models.functions import TruncDate

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.config.models import SystemSettings
from apps.payments.models import Dispute, Refund
from apps.payments.refund_service import RefundService
from apps.integrations.services import tag_zendesk_ticket_as_refunded, add_refund_comment_to_zendesk

logger = logging.getLogger(__name__)


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
