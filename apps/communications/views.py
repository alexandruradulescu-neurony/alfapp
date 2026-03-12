import logging

from rest_framework import viewsets, permissions
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend

from apps.communications.models import EmailLog
from apps.communications.serializers import EmailLogSerializer

logger = logging.getLogger(__name__)


class IsAgentOrManager(permissions.BasePermission):
    """
    Custom permission to allow only AGENT or MANAGER users.
    Explicitly validates the role value.
    """

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        if not hasattr(request.user, 'role'):
            return False
        return request.user.role in ['AGENT', 'MANAGER']

    def has_object_permission(self, request, view, obj):
        if not request.user.is_authenticated:
            return False
        if not hasattr(request.user, 'role'):
            return False
        return request.user.role in ['AGENT', 'MANAGER']


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
    filterset_fields = ['claim', 'sentiment', 'action_required']
    search_fields = ['subject', 'body', 'ai_summary']
    ordering_fields = ['received_at']
    ordering = ['-received_at']

    def get_queryset(self):
        """
        Filter queryset based on user role and query params.
        """
        queryset = super().get_queryset()
        user = self.request.user

        # Filter by claim if provided
        claim_id = self.request.query_params.get('claim_id')
        if claim_id:
            try:
                queryset = queryset.filter(claim_id=int(claim_id))
            except (ValueError, TypeError):
                logger.warning(f"Invalid claim_id parameter: {claim_id}")

        return queryset
