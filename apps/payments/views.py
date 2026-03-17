"""
DRF ViewSets for Refund API.
"""

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

from apps.payments.models import Refund
from apps.payments.serializers import (
    RefundSerializer,
    RefundListSerializer,
    RefundCreateSerializer,
    RefundStatusUpdateSerializer,
)
from apps.payments.refund_service import RefundService
from apps.users.permissions import IsManager

logger = logging.getLogger(__name__)


class PayPalWebhookView(APIView):
    """
    PayPal webhook endpoint for refund notifications.
    
    Handles PAYMENT.CAPTURE.REFUNDED and related events.
    """
    permission_classes = [AllowAny]
    
    def post(self, request):
        """Process PayPal webhook notification."""
        try:
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
    """
    
    queryset = Refund.objects.all().select_related('claim', 'created_by')
    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'refund_type', 'external_source', 'claim']
    search_fields = ['paypal_refund_id', 'claim__client_email', 'reason']
    ordering_fields = ['created_at', 'amount', 'processed_at']
    ordering = ['-created_at']
    
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
        
        refund = Refund.objects.create(
            claim=serializer.validated_data.get('claim'),
            paypal_refund_id=f'MANUAL-{Refund.objects.count() + 1}',
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
