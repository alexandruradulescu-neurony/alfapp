from rest_framework import serializers

from apps.claims.models import Claim, ClaimEvidence


class ClaimEvidenceSerializer(serializers.ModelSerializer):
    """Serializer for ClaimEvidence model."""

    class Meta:
        model = ClaimEvidence
        fields = ['id', 'claim', 'image', 'description', 'uploaded_at']
        read_only_fields = ['id', 'uploaded_at']


class ClaimSerializer(serializers.ModelSerializer):
    """Serializer for Claim model."""

    evidence_count = serializers.SerializerMethodField()

    class Meta:
        model = Claim
        fields = [
            'id',
            'client_email',
            'status',
            'zd_ticket_id',
            'flight_details',
            'created_at',
            'updated_at',
            'evidence_count',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'evidence_count']

    def get_evidence_count(self, obj):
        return obj.evidence.count()

    def validate_status(self, value):
        """Validate status is a valid choice."""
        valid_statuses = [choice[0] for choice in Claim.STATUS_CHOICES]
        if value not in valid_statuses:
            raise serializers.ValidationError(
                f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
            )
        return value

    def validate_client_email(self, value):
        """Normalize email to lowercase."""
        return value.lower()


class ClaimDetailSerializer(ClaimSerializer):
    """Detailed serializer including evidence for claim detail view."""

    evidence = ClaimEvidenceSerializer(many=True, read_only=True)

    class Meta(ClaimSerializer.Meta):
        fields = ClaimSerializer.Meta.fields + ['evidence']
