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
            'claim_id', 'amount', 'currency', 'refund_type', 'reason',
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
        """Validate amount is positive."""
        if value <= 0:
            raise serializers.ValidationError('Amount must be positive')
        return value


class RefundStatusUpdateSerializer(serializers.Serializer):
    """Serializer for updating refund status."""
    
    status = serializers.ChoiceField(choices=[
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('CANCELLED', 'Cancelled'),
    ])
    reason = serializers.CharField(required=False, allow_blank=True)
