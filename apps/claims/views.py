import logging
from django.db.models import Count

from rest_framework import serializers, viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from apps.claims.models import Claim, ClaimEvidence
from apps.claims.serializers import ClaimSerializer, ClaimDetailSerializer, ClaimEvidenceSerializer
from apps.users.permissions import IsAgentOrManager, IsManager

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
        """Only MANAGERs can delete claims."""
        if not hasattr(request.user, 'role') or request.user.role != 'MANAGER':
            return Response(
                {'detail': 'Only MANAGERS can delete claims.'},
                status=status.HTTP_403_FORBIDDEN
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error deleting claim: {e}")
            return Response(
                {'detail': 'Error deleting claim.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['patch'])
    def update_status(self, request, pk=None):
        """
        Update the status of a claim.
        Only AGENT or MANAGER can update status.
        """
        claim = self.get_object()
        new_status = request.data.get('status')

        if not new_status:
            return Response(
                {'detail': 'Status is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate status
        valid_statuses = [choice[0] for choice in Claim.STATUS_CHOICES]
        if new_status not in valid_statuses:
            return Response(
                {'detail': f'Invalid status. Must be one of: {", ".join(valid_statuses)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            claim.status = new_status
            claim.save()
            serializer = self.get_serializer(claim)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error updating claim status: {e}")
            return Response(
                {'detail': 'Error updating claim status.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

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
    """
    Update claim from Zendesk ticket.
    Fetches ticket data, analyzes with LLM, updates empty fields only, creates timeline entry.
    
    POST /api/claims/{claim_id}/update-from-zendesk/
    """
    permission_classes = [permissions.IsAuthenticated, IsAgentOrManager]
    
    def post(self, request, claim_id):
        """Update claim from Zendesk ticket."""
        try:
            claim = Claim.objects.get(id=claim_id)
            
            if not claim.zd_ticket_id:
                return Response(
                    {'error': 'No Zendesk ticket linked to this claim'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Fetch Zendesk ticket data
            from apps.integrations.services import (
                fetch_zendesk_ticket,
                fetch_zendesk_comments,
            )
            
            ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
            if not ticket_data:
                return Response(
                    {'error': 'Failed to fetch Zendesk ticket'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            
            # Fetch recent comments
            comments = fetch_zendesk_comments(claim.zd_ticket_id)
            
            # Prepare data for LLM
            claim_data = {
                'client_email': claim.client_email,
                'flight_details': claim.flight_details,
                'object_description': claim.object_description,
                'phone': claim.phone,
                'alternate_email': claim.alternate_email,
                'status': claim.status,
            }
            
            # Call LLM to analyze changes and find new information
            from apps.communications.services import call_qwen_ai
            
            llm_prompt = (
                "Analyze this Zendesk ticket update and identify new information.\n\n"
                "Existing Claim Data:\n"
                f"{json.dumps(claim_data, indent=2)}\n\n"
                f"Updated Ticket Status: {ticket_data.get('status', 'unknown')}\n"
                f"Ticket Subject: {ticket_data.get('subject', '')}\n\n"
                "Recent Comments:\n"
            )
            
            for comment in comments[:10]:
                author = comment.get('author', {}).get('name', 'Unknown')
                body = comment.get('body', '')
                llm_prompt += f"{author}: {body}\n"
            
            llm_prompt += (
                "\nReturn ONLY valid JSON in this format:\n"
                "{\n"
                '  "new_info": {\n'
                '    "flight_details": "...",\n'
                '    "object_description": "...",\n'
                '    "phone": "...",\n'
                '    "alternate_email": "..."\n'
                '  },\n'
                '  "summary": "Brief summary of what changed or was discovered"\n'
                "}\n\n"
                "Only include fields in new_info that are NEW information not already in the claim."
            )
            
            ai_result = call_qwen_ai(llm_prompt, '', ticket_data.get('subject', ''))
            
            # Parse LLM response
            import json
            import re
            
            raw_response = ai_result.get('raw_response', '')
            llm_data = None
            
            try:
                llm_data = json.loads(raw_response.strip())
            except (json.JSONDecodeError, ValueError):
                json_match = re.search(r'\{(?:[^{}]|\{[^{}]*\})*\}', raw_response, re.DOTALL)
                if json_match:
                    try:
                        llm_data = json.loads(json_match.group(0))
                    except (json.JSONDecodeError, ValueError):
                        pass
            
            if not llm_data:
                llm_data = {
                    'new_info': {},
                    'summary': 'Unable to parse LLM response',
                }
            
            # Update claim fields ONLY if they are empty (never overwrite)
            new_info = llm_data.get('new_info', {})
            updated_fields = []
            
            if new_info.get('flight_details') and not claim.flight_details:
                claim.flight_details = new_info['flight_details']
                updated_fields.append('flight_details')
            
            if new_info.get('object_description') and not claim.object_description:
                claim.object_description = new_info['object_description']
                updated_fields.append('object_description')
            
            if new_info.get('phone') and not claim.phone:
                claim.phone = new_info['phone']
                updated_fields.append('phone')
            
            if new_info.get('alternate_email') and not claim.alternate_email:
                claim.alternate_email = new_info['alternate_email']
                updated_fields.append('alternate_email')
            
            # Save claim if any fields were updated
            if updated_fields:
                claim.save()
                logger.info(
                    f"Updated claim #{claim.id} from Zendesk: {', '.join(updated_fields)}"
                )
            
            # Create timeline entry
            from apps.claims.models import ClaimUpdateTimeline
            
            update_type = 'INFO_UPDATED' if updated_fields else 'LLM_ANALYSIS'
            
            ClaimUpdateTimeline.objects.create(
                claim=claim,
                zendesk_ticket_id=claim.zd_ticket_id,
                update_type=update_type,
                changes_summary=json.dumps({
                    'updated_fields': updated_fields,
                    'ticket_status': ticket_data.get('status'),
                    'new_info': new_info,
                }),
                llm_summary=llm_data.get('summary', ''),
            )
            
            return Response({
                'success': True,
                'summary': llm_data.get('summary', 'Update completed'),
                'updated_fields': updated_fields,
            })
            
        except Claim.DoesNotExist:
            logger.warning(f"Claim {claim_id} not found for Zendesk update")
            return Response(
                {'error': 'Claim not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            logger.error(f"Error updating claim from Zendesk: {e}", exc_info=True)
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
