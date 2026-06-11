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
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'evidence_count',
            'status', 'status_category', 'status_changed_at',
        ]

    def get_evidence_count(self, obj):
        # Use annotated count if available (from ViewSet), otherwise fall back to query
        if hasattr(obj, '_evidence_count'):
            return obj._evidence_count
        return obj.evidence.count()

    def validate_client_email(self, value):
        """Normalize email to lowercase."""
        return value.lower()


class ClaimDetailSerializer(ClaimSerializer):
    """Detailed serializer including evidence for claim detail view."""

    evidence = ClaimEvidenceSerializer(many=True, read_only=True)

    class Meta(ClaimSerializer.Meta):
        fields = ClaimSerializer.Meta.fields + ['evidence']
