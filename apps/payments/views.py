"""
DRF ViewSets for Refund API.
"""

import hmac
import logging
import json
from decimal import Decimal
from typing import Dict, Any

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from django.db.models import Q, Sum
from django_filters.rest_framework import DjangoFilterBackend
from django.conf import settings

from apps.config.models import SystemSettings
from apps.payments.models import Refund
from apps.payments.serializers import (
    RefundSerializer,
    RefundListSerializer,
    RefundCreateSerializer,
    RefundStatusUpdateSerializer,
)
from apps.payments.refund_service import RefundService
from apps.users.permissions import IsManager, IsAgentOrManager

logger = logging.getLogger(__name__)


class PayPalWebhookView(APIView):
    """
    PayPal webhook endpoint for refund notifications.

    Handles PAYMENT.CAPTURE.REFUNDED and related events.

    Auth (added 2026-06-12): a mandatory X-Webhook-Secret header, checked in
    constant time against SystemSettings.sidebar_secret_token, before the
    body is parsed. This endpoint previously accepted anonymous requests and
    would record COMPLETED refunds from them — a forgery hole. In LORA's
    actual flow refunds arrive via the WooCommerce webhook, not here; this
    endpoint stays only as a secured fallback. (If you genuinely subscribe
    PayPal to post here, replace this with real PayPal signature verification
    using paypal_webhook_id.)
    """
    permission_classes = [AllowAny]  # secret-header verification below

    def post(self, request):
        """Process PayPal webhook notification."""
        try:
            webhook_secret = request.headers.get('X-Webhook-Secret', '')
            expected_secret = SystemSettings.get_instance().sidebar_secret_token or ''
            if not (webhook_secret and expected_secret
                    and hmac.compare_digest(webhook_secret.encode('utf-8'),
                                            expected_secret.encode('utf-8'))):
                logger.warning("Rejected PayPal webhook: missing or invalid X-Webhook-Secret")
                return Response({'error': 'Invalid webhook secret'},
                                status=status.HTTP_401_UNAUTHORIZED)

            # Get webhook event data
            data = request.data
            event_type = data.get('event_type', '')
            
            # Handle refund events
            if event_type in ['PAYMENT.CAPTURE.REFUNDED', 'PAYMENT.CAPTURE.REVERSED']:
                service = RefundService()
                result = service.process_webhook_refund(data)
                
                if result['success']:
                    return Response({'message': 'Webhook processed'})
                else:
                    logger.error(f"Webhook processing failed: {result.get('error')}")
                    return Response({'error': result.get('error')}, status=400)
            
            return Response({'message': 'Event type not handled'})
            
        except Exception as e:
            logger.error(f"Error processing PayPal webhook: {e}", exc_info=True)
            return Response({'error': str(e)}, status=500)


class PayPalDisputeWebhookView(APIView):
    """PayPal DISPUTE webhook — the inbound door (Phase 2).

    POST /api/payments/paypal/dispute-webhook/

    PayPal posts here directly (no shared secret), so authenticity is proven
    by PayPal's own SIGNATURE verification (verify-webhook-signature using the
    configured webhook id) — fail-closed. On CUSTOMER.DISPUTE.CREATED we fetch
    full details, create the local Dispute, match it to a claim, and capture
    the response deadline. UPDATED/RESOLVED are acknowledged here and handled
    by Phase 3 (status sync). Idempotent via ProcessedWebhookEvent.
    """
    permission_classes = [AllowAny]  # PayPal signature verification below

    def post(self, request):
        from apps.payments.paypal_disputes_service import (
            verify_webhook_signature, ingest_dispute)
        from apps.payments.models import ProcessedWebhookEvent

        event = request.data
        event_type = str(event.get('event_type', ''))
        event_id = str(event.get('id', ''))

        # 1. Authenticity — reject anything PayPal didn't sign.
        if not verify_webhook_signature(request.headers, event):
            logger.warning(f"Rejected PayPal dispute webhook (bad signature), event {event_id}")
            return Response({'error': 'Signature verification failed'},
                            status=status.HTTP_401_UNAUTHORIZED)

        # 2. Idempotency — never process the same event twice.
        if event_id and ProcessedWebhookEvent.objects.filter(event_id=event_id).exists():
            return Response({'message': 'Already processed'}, status=status.HTTP_200_OK)

        resource = event.get('resource') or {}
        dispute_id = resource.get('dispute_id') or resource.get('id') or ''

        if event_type == 'CUSTOMER.DISPUTE.CREATED' and dispute_id:
            dispute, created = ingest_dispute(dispute_id, raw_event=event)
            if dispute is None:
                # Couldn't reach PayPal to fetch details — 503 so PayPal retries.
                return Response({'error': 'Could not fetch dispute details'},
                                status=status.HTTP_503_SERVICE_UNAVAILABLE)
            if event_id:
                ProcessedWebhookEvent.objects.create(
                    event_id=event_id, event_type=event_type,
                    resource_type=event.get('resource_type', 'dispute'),
                    resource_id=dispute_id)
            return Response({'message': 'Dispute ingested', 'created': created,
                             'dispute_id': dispute.id}, status=status.HTTP_200_OK)

        # UPDATED / RESOLVED / others: acknowledge (Phase 3 handles status sync).
        if event_id:
            ProcessedWebhookEvent.objects.create(
                event_id=event_id, event_type=event_type,
                resource_type=event.get('resource_type', ''),
                resource_id=dispute_id)
        logger.info(f"PayPal dispute webhook {event_type} acknowledged (event {event_id})")
        return Response({'message': 'Acknowledged'}, status=status.HTTP_200_OK)


class RefundViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing refunds.

    list: GET /api/payments/refunds/
    create: POST /api/payments/refunds/
    retrieve: GET /api/payments/refunds/{id}/
    update: PUT /api/payments/refunds/{id}/
    partial_update: PATCH /api/payments/refunds/{id}/
    destroy: DELETE /api/payments/refunds/{id}/

    Actions:
    - process: POST /api/payments/refunds/process/
    - stats: GET /api/payments/refunds/stats/
    
    Note: The 'process' action allows AGENTs to initiate refunds from claim detail page.
    Other operations (list, create, update, delete) require MANAGER role.
    """

    queryset = Refund.objects.all().select_related('claim', 'created_by')
    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'refund_type', 'external_source', 'claim']
    search_fields = ['paypal_refund_id', 'claim__client_email', 'reason']
    ordering_fields = ['created_at', 'amount', 'processed_at']
    ordering = ['-created_at']
    # Refunds are the money audit trail. Allow GET (list/retrieve/stats) and
    # POST (manual create + process + update_status actions) only — never the
    # raw PUT/PATCH that could rewrite a COMPLETED refund's amount, nor DELETE
    # that could erase a record of money paid. (Status changes go through the
    # explicit update_status action.)
    http_method_names = ['get', 'post', 'head', 'options']

    def get_permissions(self):
        """
        Allow AGENTs to process refunds from claim detail page.
        Other operations require MANAGER role.
        """
        if self.action == 'process':
            return [IsAuthenticated(), IsAgentOrManager()]
        return super().get_permissions()
    
    def get_serializer_class(self):
        """Return appropriate serializer based on action."""
        if self.action == 'list':
            return RefundListSerializer
        elif self.action == 'create':
            return RefundCreateSerializer
        elif self.action == 'process':
            return RefundCreateSerializer
        return RefundSerializer
    
    def create(self, request, *args, **kwargs):
        """
        Create a new refund record (manual entry).
        For PayPal processing, use the 'process' action.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        import uuid
        refund = Refund.objects.create(
            claim=serializer.validated_data.get('claim'),
            paypal_refund_id=f'MANUAL-{uuid.uuid4().hex[:12]}',
            amount=serializer.validated_data['amount'],
            currency=serializer.validated_data.get('currency', 'USD'),
            status='COMPLETED',
            refund_type=serializer.validated_data['refund_type'],
            external_source='MANUAL',
            reason=serializer.validated_data['reason'],
            created_by=request.user,
        )
        
        output_serializer = RefundSerializer(refund)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['post'])
    def process(self, request):
        """
        Process a new refund via PayPal API.
        
        POST /api/payments/refunds/process/
        {
            "claim_id": 123,
            "amount": "50.00",
            "currency": "USD",
            "refund_type": "FULL",
            "reason": "Customer request"
        }
        """
        serializer = RefundCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        claim = serializer.validated_data['claim_id']
        
        # Check if claim has Zendesk ticket (for later integration)
        if not claim.zd_ticket_id:
            logger.warning(f"Claim {claim.id} has no Zendesk ticket")
        
        # Process refund via PayPal
        service = RefundService()
        result = service.initiate_refund(
            claim=claim,
            amount=serializer.validated_data['amount'],
            reason=serializer.validated_data['reason'],
            user=request.user,
            refund_type=serializer.validated_data.get('refund_type', 'FULL'),
        )
        
        if result['success']:
            output_serializer = RefundSerializer(result['refund'])
            return Response({
                'message': result['message'],
                'refund': output_serializer.data,
            }, status=status.HTTP_201_CREATED)
        else:
            return Response({
                'error': result.get('error', 'Processing failed'),
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['post'])
    def issue(self, request):
        """Issue a refund via WooCommerce (manager only) — the reverse lever.

        POST /api/payments/refunds/issue/  {claim_id, amount, reason}
        LORA → WooCommerce → PayPal → Zendesk cascade. WooCommerce is the sole
        executor; LORA records and reconciles. Manager-only (default perms).
        """
        serializer = RefundCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        claim = serializer.validated_data['claim_id']

        service = RefundService()
        result = service.issue_woocommerce_refund(
            claim=claim,
            amount=serializer.validated_data['amount'],
            reason=serializer.validated_data['reason'],
            user=request.user,
        )
        if result['success']:
            return Response({
                'message': result['message'],
                'refund': RefundSerializer(result['refund']).data,
            }, status=status.HTTP_201_CREATED)
        # Indeterminate (timeout) => 502 so the UI tells the manager to verify
        # in WooCommerce rather than blindly retry; definite failure => 400.
        code = (status.HTTP_502_BAD_GATEWAY if result.get('indeterminate')
                else status.HTTP_400_BAD_REQUEST)
        return Response({'error': result.get('error', 'Refund failed'),
                         'indeterminate': result.get('indeterminate', False)},
                        status=code)

    @action(detail=True, methods=['post'])
    def update_status(self, request, pk=None):
        """
        Update refund status manually.
        
        POST /api/payments/refunds/{id}/update_status/
        {
            "status": "COMPLETED",
            "reason": "Optional reason"
        }
        """
        refund = self.get_object()
        serializer = RefundStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        refund.status = serializer.validated_data['status']
        if serializer.validated_data.get('reason'):
            refund.metadata['status_change_reason'] = serializer.validated_data['reason']
        refund.save()
        
        output_serializer = RefundSerializer(refund)
        return Response(output_serializer.data)
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """
        Get refund statistics.
        
        GET /api/payments/refunds/stats/
        """
        from django.db.models import Sum, Count
        
        queryset = self.get_queryset()
        
        stats = {
            'total_refunds': queryset.count(),
            'total_amount': queryset.filter(status='COMPLETED').aggregate(
                total=Sum('amount')
            )['total'] or 0,
            'by_status': {
                status: queryset.filter(status=status).count()
                for status, _ in Refund.STATUS_CHOICES
            },
            'by_type': {
                refund_type: queryset.filter(refund_type=refund_type).count()
                for refund_type, _ in Refund.TYPE_CHOICES
            },
            'by_source': {
                source: queryset.filter(external_source=source).count()
                for source, _ in Refund.SOURCE_CHOICES
            },
            'recent_refunds': RefundListSerializer(
                queryset.order_by('-created_at')[:10],
                many=True
            ).data,
        }

        return Response(stats)


class DisputeScreenshotCaptureView(APIView):
    """Stub view for dispute screenshot capture."""
    permission_classes = [IsAuthenticated, IsManager]
    
    def post(self, request, dispute_id):
        return Response({'message': 'Screenshot capture not implemented'})


class ProofOfWorkPDFView(APIView):
    """Stub view for proof of work PDF generation."""
    permission_classes = [IsAuthenticated, IsManager]
    
    def get(self, request, claim_id):
        return Response({'message': 'PDF generation not implemented'})
