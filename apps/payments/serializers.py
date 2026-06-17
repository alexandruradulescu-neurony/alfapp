"""
DRF Serializers for Refund API.
"""

from rest_framework import serializers
from apps.payments.models import Refund
from apps.claims.serializers import ClaimSerializer


class RefundSerializer(serializers.ModelSerializer):
    """Serializer for Refund model."""
    
    claim = ClaimSerializer(read_only=True)
    claim_id = serializers.PrimaryKeyRelatedField(
        queryset=Refund.claim.field.related_model.objects.all(),
        source='claim',
        write_only=True,
        required=False
    )
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    refund_type_display = serializers.CharField(source='get_refund_type_display', read_only=True)
    external_source_display = serializers.CharField(source='get_external_source_display', read_only=True)
    created_by_username = serializers.CharField(source='created_by.username', read_only=True)
    
    class Meta:
        model = Refund
        fields = [
            'id', 'claim', 'claim_id',
            'paypal_refund_id', 'paypal_capture_id',
            'amount', 'currency',
            'status', 'status_display',
            'refund_type', 'refund_type_display',
            'external_source', 'external_source_display',
            'reason', 'metadata',
            'created_at', 'updated_at', 'processed_at',
            'created_by', 'created_by_username',
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'processed_at',
            'paypal_refund_id', 'metadata',
        ]


class RefundListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for refund list view."""
    
    claim_id = serializers.IntegerField(source='claim.id', read_only=True)
    claim_email = serializers.CharField(source='claim.client_email', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    refund_type_display = serializers.CharField(source='get_refund_type_display', read_only=True)
    external_source_display = serializers.CharField(source='get_external_source_display', read_only=True)
    
    class Meta:
        model = Refund
        fields = [
            'id', 'claim_id', 'claim_email',
            'paypal_refund_id', 'amount', 'currency',
            'status', 'status_display',
            'refund_type', 'refund_type_display',
            'external_source', 'external_source_display',
            'created_at',
        ]


class RefundCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating a new refund."""
    
    claim_id = serializers.IntegerField(write_only=True)
    
    class Meta:
        model = Refund
        fields = [
            # No 'currency': the business issues refunds in USD only, so the
            # client cannot choose one (the view records 'USD' unconditionally).
            'claim_id', 'amount', 'refund_type', 'reason',
        ]
    
    def validate_claim_id(self, value):
        """Validate that claim exists."""
        from apps.claims.models import Claim
        try:
            claim = Claim.objects.get(id=value)
            return claim
        except Claim.DoesNotExist:
            raise serializers.ValidationError(f'Claim {value} does not exist')
    
    def validate_amount(self, value):
        """Validate amount is positive and under the coarse absolute ceiling.
        The per-claim price_paid cap (in validate()) is the real over-refund
        guard; this catches a fat-finger when the claim has no price_paid."""
        from django.conf import settings
        from decimal import Decimal
        if value <= 0:
            raise serializers.ValidationError('Amount must be positive')
        ceiling = Decimal(str(getattr(settings, 'MAX_REFUND_AMOUNT', 100000)))
        if value > ceiling:
            raise serializers.ValidationError(
                f'Amount {value} exceeds the maximum allowed refund ({ceiling}).')
        return value

    def validate(self, attrs):
        """Defense-in-depth over-refund cap. The service (_reserve_refund) is the
        authoritative enforcer under a row lock; this rejects obvious over-refunds
        at the API boundary before any external call."""
        from decimal import Decimal
        from django.db.models import Sum
        from apps.payments.refund_service import RefundService
        claim = attrs.get('claim_id')  # validate_claim_id resolves this to a Claim
        amount = attrs.get('amount')
        if claim is not None and amount is not None and claim.price_paid:
            reserved = claim.refunds.filter(
                status__in=RefundService.RESERVING_STATUSES
            ).aggregate(t=Sum('amount'))['t'] or Decimal('0')
            remaining = claim.price_paid - reserved
            if amount > remaining:
                raise serializers.ValidationError(
                    f'Refund of {amount} exceeds the remaining refundable '
                    f'amount ({remaining}).')
        return attrs


class RefundStatusUpdateSerializer(serializers.Serializer):
    """Serializer for updating refund status."""
    
    status = serializers.ChoiceField(choices=[
        (Refund.STATUS_PENDING, 'Pending'),
        (Refund.STATUS_PROCESSING, 'Processing'),
        (Refund.STATUS_COMPLETED, 'Completed'),
        (Refund.STATUS_FAILED, 'Failed'),
        (Refund.STATUS_CANCELLED, 'Cancelled'),
    ])
    reason = serializers.CharField(required=False, allow_blank=True)
