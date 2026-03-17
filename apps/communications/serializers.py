from rest_framework import serializers

from apps.communications.models import EmailLog


class EmailLogSerializer(serializers.ModelSerializer):
    """Serializer for EmailLog model."""

    claim_id = serializers.IntegerField(source='claim.id', read_only=True)
    claim_status = serializers.CharField(source='claim.status', read_only=True)

    class Meta:
        model = EmailLog
        fields = [
            'id',
            'claim_id',
            'claim_status',
            'subject',
            'body',
            'ai_summary',
            'action_required',
            'category',
            'auto_resolved',
            'received_at',
        ]
        read_only_fields = [
            'id',
            'claim_id',
            'claim_status',
            'ai_summary',
            'action_required',
            'received_at',
        ]
