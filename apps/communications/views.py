import logging

from rest_framework import viewsets, permissions
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend

from apps.communications.models import EmailLog
from apps.communications.serializers import EmailLogSerializer
from apps.users.permissions import IsAgentOrManager

logger = logging.getLogger(__name__)


class EmailLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing email logs.

    Read-only for AGENT and MANAGER.
    Emails are logged automatically from IMAP integration.
    """

    queryset = EmailLog.objects.all()
    serializer_class = EmailLogSerializer
    permission_classes = [permissions.IsAuthenticated, IsAgentOrManager]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    # NOTE: 'sentiment' was listed here for years but never existed on the
    # model — django-filter raises on first request, 500ing this endpoint.
    filterset_fields = ['claim', 'category', 'action_required']
    search_fields = ['subject', 'body', 'ai_summary']
    ordering_fields = ['received_at']
    ordering = ['-received_at']

    def get_queryset(self):
        """
        Filter queryset based on user role and query params.
        Optimized to defer heavy text fields for list operations.
        """
        queryset = super().get_queryset()
        user = self.request.user

        # Defer heavy text fields for list operations to reduce payload size
        if self.action == 'list':
            queryset = queryset.defer('body', 'raw_headers', 'ai_summary')

        # Filter by claim if provided
        claim_id = self.request.query_params.get('claim_id')
        if claim_id:
            try:
                queryset = queryset.filter(claim_id=int(claim_id))
            except (ValueError, TypeError):
                logger.warning(f"Invalid claim_id parameter: {claim_id}")

        return queryset
