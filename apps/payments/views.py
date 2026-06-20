"""
DRF ViewSets for Refund API.
"""

import logging
import json
import uuid
from decimal import Decimal
from typing import Dict, Any

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from django.db.models import Count, Q, Sum
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

logger = logging.getLogger(__name__)


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
            verify_webhook_signature, ingest_dispute, sync_dispute_from_paypal)
        from apps.payments.models import ProcessedWebhookEvent

        event = request.data
        event_type = str(event.get('event_type', ''))
        event_id = str(event.get('id', ''))

        # 1. Authenticity — reject anything PayPal didn't sign.
        if not verify_webhook_signature(request.headers, event):
            logger.warning(f"Rejected PayPal dispute webhook (bad signature), event {event_id}")
            return Response({'error': 'Signature verification failed'},
                            status=status.HTTP_401_UNAUTHORIZED)

        resource = event.get('resource') or {}
        dispute_id = resource.get('dispute_id') or resource.get('id') or ''

        # 2. Idempotency — atomically CLAIM the event BEFORE any side effects, so
        # concurrent retries can't both process it (the old check-then-create left
        # a TOCTOU window and could double-run + 500 on the duplicate insert). The
        # unique event_id makes get_or_create the single source of truth.
        if event_id:
            _, created_gate = ProcessedWebhookEvent.objects.get_or_create(
                event_id=event_id,
                defaults={'event_type': event_type,
                          'resource_type': event.get('resource_type', '') or 'dispute',
                          'resource_id': dispute_id})
            if not created_gate:
                return Response({'message': 'Already processed'}, status=status.HTTP_200_OK)

        # Run the side effects under a guard: if ANYTHING fails (PayPal
        # unreachable OR an exception mid-processing), RELEASE the idempotency
        # claim and return 5xx so PayPal's retry can reprocess. Otherwise a
        # raised exception would leave the event marked processed and the retry
        # would short-circuit ("Already processed"), permanently dropping it.
        try:
            if event_type == 'CUSTOMER.DISPUTE.CREATED' and dispute_id:
                dispute, created = ingest_dispute(dispute_id, raw_event=event)
                if dispute is None:
                    raise RuntimeError('ingest_dispute returned no dispute (PayPal unreachable)')
                return Response({'message': 'Dispute ingested', 'created': created,
                                 'dispute_id': dispute.id}, status=status.HTTP_200_OK)

            # UPDATED / RESOLVED: refresh the local dispute (stage, deadline, and
            # won/lost on resolution).
            if event_type in ('CUSTOMER.DISPUTE.UPDATED', 'CUSTOMER.DISPUTE.RESOLVED') and dispute_id:
                sync_dispute_from_paypal(dispute_id)
        except Exception as e:
            logger.error(
                f"PayPal dispute webhook side-effect failed (event {event_id}, {event_type}): {e}")
            if event_id:
                ProcessedWebhookEvent.objects.filter(event_id=event_id).delete()
            return Response({'error': 'Processing failed; will retry'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

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

    Auth: every action requires only authentication. (The former AGENT vs MANAGER
    role distinction was removed — single trusted-staff user model — so there is
    no per-action permission split anymore.)
    """

    queryset = Refund.objects.all().select_related('claim', 'created_by')
    permission_classes = [IsAuthenticated]
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

        The money-writing logic lives in RefundService.create_manual_refund (thin
        view), which is also idempotent within a short window so a double-submit
        doesn't insert a duplicate money record.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        refund = RefundService().create_manual_refund(
            # validate_claim_id resolves 'claim_id' to a Claim instance.
            claim=serializer.validated_data.get('claim_id'),
            amount=serializer.validated_data['amount'],
            currency='USD',  # the business issues refunds in USD only
            refund_type=serializer.validated_data['refund_type'],
            reason=serializer.validated_data['reason'],
            user=request.user,
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
        
        new_status = serializer.validated_data['status']
        reason = serializer.validated_data.get('reason')
        # Route through the model transitions so side effects are applied — a raw
        # status set to COMPLETED used to leave processed_at empty.
        if new_status == Refund.STATUS_COMPLETED:
            refund.mark_completed()
        elif new_status == Refund.STATUS_FAILED:
            refund.mark_failed(reason or '')   # folds reason into metadata itself
        elif new_status == Refund.STATUS_PROCESSING:
            refund.mark_processing()
        elif new_status == Refund.STATUS_CANCELLED:
            refund.mark_cancelled()
        else:
            refund.status = new_status
            refund.save(update_fields=['status', 'updated_at'])

        # Persist the optional human reason. mark_failed already records it; for
        # the other transitions the mark_* saves are field-scoped (don't touch
        # metadata), so write it here in its own scoped save.
        if reason and new_status != Refund.STATUS_FAILED:
            refund.metadata['status_change_reason'] = reason
            refund.save(update_fields=['metadata', 'updated_at'])

        output_serializer = RefundSerializer(refund)
        return Response(output_serializer.data)
    
    @action(detail=True, methods=['post'])
    def reconcile(self, request, pk=None):
        """Reconcile a stuck PENDING WooCommerce refund against WooCommerce.

        POST /api/payments/refunds/{id}/reconcile/
        Pulls the order's actual refunds from WooCommerce (read-only) and, if one
        matches this row's amount, marks it COMPLETED — the self-heal for a refund
        that LORA issued but couldn't confirm (the call timed out). Moves no money.
        """
        refund = self.get_object()
        result = RefundService().reconcile_woocommerce_refund(refund)
        if not result['success']:
            code = (status.HTTP_502_BAD_GATEWAY if result.get('indeterminate')
                    else status.HTTP_400_BAD_REQUEST)
            return Response({'error': result.get('error', 'Could not reconcile'),
                             'indeterminate': result.get('indeterminate', False)}, status=code)
        if result.get('reconciled'):
            return Response({'message': 'Refund confirmed in WooCommerce and marked completed.',
                             'refund': RefundSerializer(result['refund']).data})
        # Reached WooCommerce, but nothing to mark completed — report honestly.
        return Response({'message': result.get('message', 'No matching WooCommerce refund found.'),
                         'reconciled': False,
                         'found': result.get('found', True)}, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """
        Get refund statistics.
        
        GET /api/payments/refunds/stats/
        """
        queryset = self.get_queryset()

        # One aggregate query instead of 1 + 6 + 2 + 3 separate COUNTs. Building
        # the dicts from the *_CHOICES lists (not from the rows) keeps every key,
        # including choices with zero refunds.
        agg = queryset.aggregate(
            total_refunds=Count('id'),
            total_amount=Sum('amount', filter=Q(status=Refund.STATUS_COMPLETED)),
            **{f'st_{s}': Count('id', filter=Q(status=s)) for s, _ in Refund.STATUS_CHOICES},
            **{f'ty_{t}': Count('id', filter=Q(refund_type=t)) for t, _ in Refund.TYPE_CHOICES},
            **{f'so_{x}': Count('id', filter=Q(external_source=x)) for x, _ in Refund.SOURCE_CHOICES},
        )
        stats = {
            'total_refunds': agg['total_refunds'],
            'total_amount': agg['total_amount'] or 0,
            'by_status': {s: agg[f'st_{s}'] for s, _ in Refund.STATUS_CHOICES},
            'by_type': {t: agg[f'ty_{t}'] for t, _ in Refund.TYPE_CHOICES},
            'by_source': {x: agg[f'so_{x}'] for x, _ in Refund.SOURCE_CHOICES},
            'recent_refunds': RefundListSerializer(
                queryset.order_by('-created_at')[:10],
                many=True
            ).data,
        }

        return Response(stats)


class ProofOfWorkPDFView(APIView):
    """Stub view for proof of work PDF generation."""
    permission_classes = [IsAuthenticated]
    
    def get(self, request, claim_id):
        return Response({'message': 'PDF generation not implemented'})
