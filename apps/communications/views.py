import logging

from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
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
        # select_related('claim'): the serializer reads claim.id/claim.status, so
        # without this a list of N emails fires N extra Claim queries (N+1).
        queryset = super().get_queryset().select_related('claim')
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

    @action(detail=True, methods=['post'])
    def resolve(self, request, pk=None):
        """Mark an email handled, or reopen it (agent/manager).

        POST /api/communications/email-logs/{id}/resolve/  {resolved: bool}
        Clears (or restores) the 'action required' flag — purely a LORA-side
        housekeeping toggle so handled institution mail stops showing as
        needing attention and drops out of the manager dashboard's
        "Emails need a reply" count. Does NOT touch the ticket, the claim
        status, or the email's read state in the shared inbox.
        """
        email_log = self.get_object()
        # Parse robustly — a bare bool() makes the string "false" truthy.
        from rest_framework.fields import BooleanField
        resolved = request.data.get('resolved', True) in BooleanField.TRUE_VALUES
        email_log.action_required = not resolved
        email_log.save(update_fields=['action_required'])
        logger.info(
            f"EmailLog #{email_log.id} marked "
            f"{'resolved' if resolved else 'needs-attention'} by {request.user.username}")
        return Response({'id': email_log.id,
                         'action_required': email_log.action_required},
                        status=status.HTTP_200_OK)
