import json
import logging
from django.db import transaction
from django.db.models import Count, ProtectedError
from django.shortcuts import get_object_or_404

from rest_framework import serializers, viewsets, permissions, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from apps.claims.models import Claim, ClaimEvidence, ClaimUpdateTimeline
from apps.claims.serializers import ClaimSerializer, ClaimDetailSerializer, ClaimEvidenceSerializer
from apps.claims.services import refresh_claim_from_zendesk
from apps.users.permissions import IsAgentOrManager
from apps.integrations.services import (
    fetch_zendesk_ticket,
    fetch_zendesk_comments,
    analyze_zendesk_ticket_for_claim,
    get_ticket_email_alias,
)
from apps.integrations.briefing import refresh_claim_summary

logger = logging.getLogger(__name__)


class ClaimViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing claims.

    A single trusted authenticated user type has full access (list, retrieve,
    create, update, delete) — there is no agent/manager role split.
    """

    queryset = Claim.objects.all()
    permission_classes = [permissions.IsAuthenticated, IsAgentOrManager]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'client_email']
    search_fields = ['client_email', 'zd_ticket_id', 'flight_details']
    ordering_fields = ['created_at', 'updated_at', 'status']
    ordering = ['-created_at']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ClaimDetailSerializer
        return ClaimSerializer

    def get_queryset(self):
        """All claims, with N+1-avoiding prefetch/annotate. Single trusted user
        type — no per-user scoping."""
        return super().get_queryset().select_related('assigned_to').prefetch_related(
            'evidence',
            'emails'
        ).annotate(_evidence_count=Count('evidence', distinct=True))

    def destroy(self, request, *args, **kwargs):
        """Delete a claim (e.g. a junk ticket that slipped in).

        Timeline and evidence rows cascade away with the claim. Processed
        emails are kept for audit but detached (their claim link cleared).
        Refunds and disputes PROTECT the claim — a claim with money records
        attached refuses deletion with a clear message.
        """
        claim = self.get_object()
        try:
            with transaction.atomic():
                claim.emails.update(claim=None)
                claim.delete()
        except ProtectedError:
            return Response(
                {'detail': 'This claim has refunds or disputes attached and '
                           'cannot be deleted.'},
                status=status.HTTP_409_CONFLICT
            )
        except Exception as e:
            logger.error("Error deleting claim: %s", e, exc_info=True)
            return Response(
                {'detail': 'Error deleting claim.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        logger.info("Claim #%s deleted by %s", kwargs.get('pk'), request.user.username)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        """POST /api/claims/claims/bulk-delete/  Body: {ids: [..]}

        Bulk cleanup (junk phone/email-ticket claims) for the single trusted
        authenticated user. Same semantics as single delete, per claim: emails
        detached, timeline and evidence cascade, refunds/disputes block —
        blocked claims are skipped and reported back, never silently kept or
        silently lost.
        """
        ids = request.data.get('ids')
        if not isinstance(ids, list) or not ids or \
                not all(str(i).isdigit() for i in ids):
            return Response({'detail': 'Send {"ids": [claim ids]}.'},
                            status=status.HTTP_400_BAD_REQUEST)
        ids = [int(i) for i in ids]

        deleted, blocked = [], []
        # Per-claim transaction boundary is intentional: each claim commits (or is
        # skipped) independently so one PROTECTED claim can't roll back the rest.
        # A mid-sweep crash leaves the already-deleted claims committed; the
        # response reports exactly what was deleted vs blocked.
        for claim in Claim.objects.filter(id__in=ids):
            claim_id = claim.id
            try:
                with transaction.atomic():
                    claim.emails.update(claim=None)
                    claim.delete()
                deleted.append(claim_id)
            except ProtectedError:
                blocked.append(claim_id)
        logger.info("Bulk claim delete by %s: deleted=%s, blocked=%s",
                    request.user.username, deleted, blocked)
        return Response({'deleted': deleted, 'blocked': blocked})

    @action(detail=True, methods=['get'], url_path='proof-of-work')
    def proof_of_work(self, request, pk=None):
        """Generate and download the proof-of-work PDF for a claim."""
        claim = self.get_object()
        
        try:
            from apps.payments.utils import generate_proof_of_work_pdf
            from django.http import HttpResponse
            
            pdf_bytes = generate_proof_of_work_pdf(claim)
            
            if not pdf_bytes:
                return Response(
                    {'detail': 'Failed to generate PDF.'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            response = HttpResponse(pdf_bytes, content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="proof_of_work_claim_{claim.id}.pdf"'
            response['Content-Length'] = len(pdf_bytes)
            
            logger.info("Proof of work PDF downloaded for claim #%s by %s", claim.id, request.user)
            return response

        except Exception as e:
            logger.error("Error generating proof of work PDF: %s", e, exc_info=True)
            return Response(
                {'detail': 'Error generating PDF.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ClaimEvidenceViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing claim evidence.

    A single trusted authenticated user type has full access (create, list,
    delete) — there is no agent/manager role split.
    """

    queryset = ClaimEvidence.objects.all()
    serializer_class = ClaimEvidenceSerializer
    permission_classes = [permissions.IsAuthenticated, IsAgentOrManager]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['claim']
    ordering_fields = ['uploaded_at']
    ordering = ['-uploaded_at']

    def get_queryset(self):
        """Filter evidence by claim if provided."""
        queryset = super().get_queryset()
        claim_id = self.request.query_params.get('claim')
        if claim_id:
            try:
                queryset = queryset.filter(claim_id=int(claim_id))
            except (ValueError, TypeError):
                pass
        return queryset

    def perform_create(self, serializer):
        """
        Associate evidence with a claim. Mirrors the frontend upload path: the
        image is size/type validated (the API previously did neither). Any
        authenticated staff member may upload to any claim — there is no per-user
        ownership check (the manager/agent role split was removed).
        """
        from django.core.exceptions import ValidationError as DjangoValidationError
        from rest_framework.exceptions import PermissionDenied
        from apps.claims.services import validate_evidence_image

        claim_id = self.request.data.get('claim')
        if not claim_id:
            raise serializers.ValidationError({'claim': 'Claim ID is required.'})
        try:
            claim = Claim.objects.get(id=claim_id)
        except (Claim.DoesNotExist, ValueError, TypeError):
            logger.warning("Claim %s not found for evidence upload", claim_id)
            raise serializers.ValidationError({'claim': 'Claim not found.'})


        try:
            validate_evidence_image(serializer.validated_data.get('image'))
        except DjangoValidationError as e:
            raise serializers.ValidationError({'image': e.messages})

        serializer.save(claim=claim)

    def destroy(self, request, *args, **kwargs):
        """Delete a piece of claim evidence."""
        try:
            return super().destroy(request, *args, **kwargs)
        except Exception as e:
            logger.error("Error deleting evidence: %s", e, exc_info=True)
            return Response(
                {'detail': 'Error deleting evidence.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ClaimUpdateFromZendeskView(APIView):
    """POST /api/claims/{claim_id}/update-from-zendesk/

    Re-extracts ALL claim facts from the live ticket and regenerates the AI
    summary. Values read from structured Zendesk fields overwrite the claim
    (Zendesk is the source of truth); LLM-inferred values fill blanks only.
    Never touches claim.status — the webhook owns the stage mirror."""

    authentication_classes = [SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, claim_id):

        claim = get_object_or_404(Claim, id=claim_id)
        if not claim.zd_ticket_id:
            return Response({'error': 'No Zendesk ticket linked to this claim'},
                            status=status.HTTP_400_BAD_REQUEST)

        ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
        if not ticket_data:
            return Response({'error': 'Failed to fetch Zendesk ticket'},
                            status=status.HTTP_502_BAD_GATEWAY)
        ticket_data['comments'] = fetch_zendesk_comments(claim.zd_ticket_id)

        extracted = analyze_zendesk_ticket_for_claim(ticket_data)
        # AI summary + risk detection are best-effort (their own save); run BEFORE
        # the atomic block so the LLM call never holds a DB transaction open.
        # Trade-off: the summary's PII-alias hint uses the pre-merge client_name;
        # that's accepted — it only seeds the tokenizer's known-names optimisation
        # (the tokenizer's own regex still tags the name), so a renamed client is
        # not a PII leak.  Risk detection must always run — a new hostile comment
        # can carry risk even when no structured fields changed.
        summary_refreshed = refresh_claim_summary(claim, ticket_data)

        # The field-merge save and the timeline row are one unit: a crash between
        # them must not leave an updated claim with no history entry.
        with transaction.atomic():
            updated_fields = refresh_claim_from_zendesk(claim, extracted)
            if updated_fields:
                pretty = ", ".join(f.replace('_', ' ') for f in updated_fields)
                ClaimUpdateTimeline.objects.create(
                    claim=claim,
                    zendesk_ticket_id=claim.zd_ticket_id,
                    update_type='INFO_UPDATED',
                    changes_summary=json.dumps({'updated_fields': updated_fields}),
                    llm_summary=f"Updated: {pretty}.",
                )
        logger.info("Refreshed claim #%s from Zendesk: %s", claim.id, updated_fields)
        return Response({
            'message': 'Claim refreshed from Zendesk',
            'updated_fields': updated_fields,
            'summary_refreshed': summary_refreshed,
        })


class ClaimCheckEmailView(APIView):
    """POST /api/claims/{claim_id}/check-email/

    Checks the shared mailbox for new mail addressed to THIS claim's email
    alias only (unread, last 2 days, never processed before). New mail gets
    AI categorization, an EmailLog row, an internal note on the Zendesk
    ticket and additive ai_* tags. The rest of the inbox is untouched."""

    authentication_classes = [SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, claim_id):
        from apps.communications.services import (
            EmailNotConfigured, InvalidAlias, check_email_for_ticket)


        claim = get_object_or_404(Claim, id=claim_id)
        if not claim.zd_ticket_id:
            return Response({'error': 'No Zendesk ticket linked to this claim'},
                            status=status.HTTP_400_BAD_REQUEST)

        alias = claim.email_alias
        if not alias:
            ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
            if not ticket_data:
                return Response({'error': 'Failed to fetch Zendesk ticket'},
                                status=status.HTTP_502_BAD_GATEWAY)
            alias = get_ticket_email_alias(ticket_data)
            if not alias:
                return Response(
                    {'error': "This ticket has no email alias field in Zendesk — "
                              "there is no address to check mail for."},
                    status=status.HTTP_400_BAD_REQUEST)
            claim.email_alias = alias
            claim.save(update_fields=['email_alias', 'updated_at'])

        try:
            results = check_email_for_ticket(claim.zd_ticket_id, claim, alias)
        except EmailNotConfigured:
            return Response(
                {'error': 'Mailbox (IMAP) credentials are not configured in System settings.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except InvalidAlias:
            return Response(
                {'error': "The ticket's email alias doesn't look like an email "
                          "address — fix the Email Alias field in Zendesk."},
                status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error("Email check failed for claim #%s: %s", claim.id, e, exc_info=True)
            return Response({'error': 'Could not reach the mailbox. Try again.'},
                            status=status.HTTP_502_BAD_GATEWAY)

        new_count = len(results['processed'])
        logger.info("Email check for claim #%s (%s): %s new, %s already processed",
                    claim.id, alias, new_count, results['already_processed'])
        return Response({'message': f"{new_count} new email(s) processed", **results})
