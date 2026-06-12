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


class CsrfExemptSessionAuthentication(SessionAuthentication):
    """Session authentication that doesn't enforce CSRF."""
    def enforce_csrf(self, request):
        # Don't enforce CSRF for API endpoints
        pass

from apps.claims.models import Claim, ClaimEvidence, ClaimUpdateTimeline
from apps.claims.serializers import ClaimSerializer, ClaimDetailSerializer, ClaimEvidenceSerializer
from apps.claims.services import compute_deadline_at
from apps.users.permissions import IsAgentOrManager
from apps.integrations.services import (
    fetch_zendesk_ticket,
    fetch_zendesk_comments,
    analyze_zendesk_ticket_for_claim,
    get_ticket_email_alias,
    safe_date,
    safe_decimal,
)
from apps.integrations.briefing import refresh_claim_summary

logger = logging.getLogger(__name__)


class ClaimViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing claims.

    - AGENT: Can list, retrieve, update claims
    - MANAGER: Full access (create, update, delete)
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
        """
        Filter queryset based on user role.
        Optimized to prevent N+1 queries with select_related, prefetch_related, and annotate.
        MANAGERs see all claims, AGENTs see all claims (can be modified for multi-tenant).
        """
        queryset = super().get_queryset()
        user = self.request.user

        # Optimize queries: 
        # - select_related for FK (assigned_to)
        # - prefetch_related for reverse FK (evidence, emails)
        # - annotate evidence_count to avoid N+1 in serializer
        queryset = queryset.select_related('assigned_to').prefetch_related(
            'evidence',
            'emails'
        ).annotate(_evidence_count=Count('evidence', distinct=True))

        if hasattr(user, 'role') and user.role == 'AGENT':
            # AGENTs can see all claims (adjust if needed for tenant isolation)
            return queryset
        return queryset

    def create(self, request, *args, **kwargs):
        """Only MANAGERs can create claims."""
        if not hasattr(request.user, 'role') or request.user.role != 'MANAGER':
            return Response(
                {'detail': 'Only MANAGERS can create claims.'},
                status=status.HTTP_403_FORBIDDEN
            )
        return super().create(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """Only MANAGERs can delete claims (e.g. junk tickets that slipped in).

        Timeline and evidence rows cascade away with the claim. Processed
        emails are kept for audit but detached (their claim link cleared).
        Refunds and disputes PROTECT the claim — a claim with money records
        attached refuses deletion with a clear message.
        """
        if not hasattr(request.user, 'role') or request.user.role != 'MANAGER':
            return Response(
                {'detail': 'Only MANAGERS can delete claims.'},
                status=status.HTTP_403_FORBIDDEN
            )
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
            logger.error(f"Error deleting claim: {e}")
            return Response(
                {'detail': 'Error deleting claim.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        logger.info(f"Claim #{kwargs.get('pk')} deleted by {request.user.username}")
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        """POST /api/claims/claims/bulk-delete/  Body: {ids: [..]}

        Manager-only bulk cleanup (junk phone/email-ticket claims). Same
        semantics as single delete, per claim: emails detached, timeline and
        evidence cascade, refunds/disputes block — blocked claims are skipped
        and reported back, never silently kept or silently lost.
        """
        if not hasattr(request.user, 'role') or request.user.role != 'MANAGER':
            return Response(
                {'detail': 'Only MANAGERS can delete claims.'},
                status=status.HTTP_403_FORBIDDEN
            )
        ids = request.data.get('ids')
        if not isinstance(ids, list) or not ids or \
                not all(str(i).isdigit() for i in ids):
            return Response({'detail': 'Send {"ids": [claim ids]}.'},
                            status=status.HTTP_400_BAD_REQUEST)
        ids = [int(i) for i in ids]

        deleted, blocked = [], []
        for claim in Claim.objects.filter(id__in=ids):
            claim_id = claim.id
            try:
                with transaction.atomic():
                    claim.emails.update(claim=None)
                    claim.delete()
                deleted.append(claim_id)
            except ProtectedError:
                blocked.append(claim_id)
        logger.info(f"Bulk claim delete by {request.user.username}: "
                    f"deleted={deleted}, blocked={blocked}")
        return Response({'deleted': deleted, 'blocked': blocked})

    @action(detail=True, methods=['get'], url_path='proof-of-work')
    def proof_of_work(self, request, pk=None):
        """
        Generate and download proof of work PDF for a claim.
        Only MANAGERs can access this endpoint.
        """
        # Check if user is MANAGER
        if not hasattr(request.user, 'role') or request.user.role != 'MANAGER':
            return Response(
                {'detail': 'Only MANAGERS can download proof of work PDFs.'},
                status=status.HTTP_403_FORBIDDEN
            )
        
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
            
            logger.info(f"Proof of work PDF downloaded for claim #{claim.id} by {request.user}")
            return response
            
        except Exception as e:
            logger.error(f"Error generating proof of work PDF: {e}")
            return Response(
                {'detail': 'Error generating PDF.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class ClaimEvidenceViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing claim evidence.

    - AGENT: Can create and list evidence
    - MANAGER: Full access
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
        Associate evidence with a claim.
        Only AGENT or MANAGER can upload evidence.
        """
        try:
            claim_id = self.request.data.get('claim')
            if not claim_id:
                raise serializers.ValidationError({'claim': 'Claim ID is required.'})

            claim = Claim.objects.get(id=claim_id)
            serializer.save(claim=claim)
        except Claim.DoesNotExist:
            logger.warning(f"Claim {claim_id} not found for evidence upload")
            raise serializers.ValidationError({'claim': 'Claim not found.'})
        except Exception as e:
            logger.error(f"Error uploading evidence: {e}")
            raise serializers.ValidationError({'detail': 'Error uploading evidence.'})

    def destroy(self, request, *args, **kwargs):
        """Only MANAGERs can delete evidence."""
        if not hasattr(request.user, 'role') or request.user.role != 'MANAGER':
            return Response(
                {'detail': 'Only MANAGERS can delete evidence.'},
                status=status.HTTP_403_FORBIDDEN
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error deleting evidence: {e}")
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

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    OVERWRITE_FIELDS = [
        'client_email', 'client_name', 'flight_details', 'phone',
        'billing_address', 'shipping_address', 'incident_details',
        'lost_location', 'deadline_time', 'deadline_timezone',
        'payment_method', 'payment_status', 'woocommerce_id', 'tracking_info',
    ]
    FILL_ONLY_FIELDS = [
        'object_description',
        'alternate_email',  # alternate_email: extractor returns '' today — reserved for when extraction adds it
    ]

    def post(self, request, claim_id):
        if not hasattr(request.user, 'role') or request.user.role not in ['AGENT', 'MANAGER']:
            return Response({'error': 'Permission denied: AGENT or MANAGER role required'},
                            status=status.HTTP_403_FORBIDDEN)

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

        updated_fields = []
        for field in self.OVERWRITE_FIELDS:
            value = (extracted.get(field) or '').strip()
            if value and value != (getattr(claim, field) or ''):
                setattr(claim, field, value)
                updated_fields.append(field)
        for field in self.FILL_ONLY_FIELDS:
            value = (extracted.get(field) or '').strip()
            if value and not (getattr(claim, field) or ''):
                setattr(claim, field, value)
                updated_fields.append(field)

        new_date = safe_date(extracted.get('deadline_date', ''))
        if new_date and new_date != claim.deadline_date:
            claim.deadline_date = new_date
            updated_fields.append('deadline_date')
        new_price = safe_decimal(extracted.get('price_paid', ''))
        if new_price is not None and new_price != claim.price_paid:
            claim.price_paid = new_price
            updated_fields.append('price_paid')

        claim.deadline_at = compute_deadline_at(
            claim.deadline_date, claim.deadline_time, claim.deadline_timezone)
        save_fields = set(updated_fields) | {'deadline_at', 'updated_at'}
        claim.save(update_fields=list(save_fields))

        summary_refreshed = refresh_claim_summary(claim, ticket_data)

        ClaimUpdateTimeline.objects.create(
            claim=claim,
            zendesk_ticket_id=claim.zd_ticket_id,
            update_type='INFO_UPDATED',
            changes_summary=json.dumps({'updated_fields': updated_fields}),
            llm_summary=claim.ai_summary if summary_refreshed else '',
        )
        logger.info(f"Refreshed claim #{claim.id} from Zendesk: {updated_fields}")
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

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, claim_id):
        from apps.communications.services import (
            EmailNotConfigured, InvalidAlias, check_email_for_ticket)

        if not hasattr(request.user, 'role') or request.user.role not in ['AGENT', 'MANAGER']:
            return Response({'error': 'Permission denied: AGENT or MANAGER role required'},
                            status=status.HTTP_403_FORBIDDEN)

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
            logger.error(f"Email check failed for claim #{claim.id}: {e}", exc_info=True)
            return Response({'error': 'Could not reach the mailbox. Try again.'},
                            status=status.HTTP_502_BAD_GATEWAY)

        new_count = len(results['processed'])
        logger.info(f"Email check for claim #{claim.id} ({alias}): "
                    f"{new_count} new, {results['already_processed']} already processed")
        return Response({'message': f"{new_count} new email(s) processed", **results})
