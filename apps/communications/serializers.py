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
            'sentiment',
            'action_required',
            'received_at',
        ]
        read_only_fields = [
            'id',
            'claim_id',
            'claim_status',
            'ai_summary',
            'sentiment',
            'action_required',
            'received_at',
        ]

    def validate_sentiment(self, value):
        """Validate sentiment is a valid choice."""
        if value:
            valid_sentiments = [choice[0] for choice in EmailLog.SENTIMENT_CHOICES]
            if value not in valid_sentiments:
                raise serializers.ValidationError(
                    f"Invalid sentiment. Must be one of: {', '.join(valid_sentiments)}"
                )
        return value
