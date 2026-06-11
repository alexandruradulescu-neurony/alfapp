import json
import logging
from django.db.models import Count

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
            
            log(f"Proof of work PDF downloaded for claim #{claim.id} by {request.user}")
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
    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, claim_id):
        """Update claim from Zendesk ticket."""
        # File-based logging for debugging
        import os
        from datetime import datetime
        log_file = os.path.join(os.path.dirname(__file__), 'update_zendesk_debug.log')
        debug_log = []
        
        def log(msg):
            debug_log.append(f"{datetime.now().isoformat()} - {msg}")
            logger.info(msg)
        
        log(f"=== UPDATE FROM ZENDESK STARTED ===")
        log(f"Claim ID: {claim_id}, User: {request.user}")

        # Check role manually
        if not hasattr(request.user, 'role') or request.user.role not in ['AGENT', 'MANAGER']:
            log(f"Permission denied: User role '{getattr(request.user, 'role', 'none')}' not allowed")
            with open(log_file, 'a') as f:
                f.write('\n'.join(debug_log) + '\n')
            return Response(
                {'error': 'Permission denied: AGENT or MANAGER role required'},
                status=status.HTTP_403_FORBIDDEN
            )

        log(f"Authentication passed for user: {request.user.username}, role: {request.user.role}")

        try:
            claim = Claim.objects.get(id=claim_id)
            log(f"Found claim #{claim.id}, Zendesk Ticket: {claim.zd_ticket_id}")

            if not claim.zd_ticket_id:
                log(f"Claim #{claim_id} has no Zendesk ticket linked")
                with open(log_file, 'a') as f:
                    f.write('\n'.join(debug_log) + '\n\n')
                return Response(
                    {'error': 'No Zendesk ticket linked to this claim'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Fetch Zendesk ticket data
            from apps.integrations.services import (
                fetch_zendesk_ticket,
                fetch_zendesk_comments,
            )

            log(f"Fetching Zendesk ticket {claim.zd_ticket_id}...")
            ticket_data = fetch_zendesk_ticket(claim.zd_ticket_id)
            if not ticket_data:
                log(f"Failed to fetch Zendesk ticket {claim.zd_ticket_id}")
                with open(log_file, 'a') as f:
                    f.write('\n'.join(debug_log) + '\n\n')
                return Response(
                    {'error': 'Failed to fetch Zendesk ticket'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
            log(f"Fetched ticket data: {ticket_data}")

            # Fetch recent comments
            log(f"Fetching comments for ticket {claim.zd_ticket_id}...")
            comments = fetch_zendesk_comments(claim.zd_ticket_id)
            log(f"Fetched {len(comments)} comments")
            
            # Prepare data for LLM
            claim_data = {
                'client_email': claim.client_email,
                'flight_details': claim.flight_details,
                'object_description': claim.object_description,
                'phone': claim.phone,
                'alternate_email': claim.alternate_email,
                'status': claim.status,
            }
            
            # Add comments to ticket data for LLM analysis
            ticket_data['comments'] = comments

            # Call LLM to analyze changes and find new information
            from apps.integrations.services import analyze_zendesk_ticket_for_claim

            log(f"Calling LLM to analyze ticket...")
            extracted_data = analyze_zendesk_ticket_for_claim(ticket_data)
            log(f"LLM extraction completed: {extracted_data}")

            # Compare with existing claim data to find NEW information
            new_info = {}

            if extracted_data.get('flight_details') and not claim.flight_details:
                new_info['flight_details'] = extracted_data['flight_details']

            if extracted_data.get('object_description') and not claim.object_description:
                new_info['object_description'] = extracted_data['object_description']

            if extracted_data.get('phone') and not claim.phone:
                new_info['phone'] = extracted_data['phone']

            if extracted_data.get('alternate_email') and not claim.alternate_email:
                new_info['alternate_email'] = extracted_data['alternate_email']

            log(f"New info from LLM: {new_info}")
            log(f"Current claim data - flight: {bool(claim.flight_details)}, object: {bool(claim.object_description)}, phone: {bool(claim.phone)}, alt_email: {bool(claim.alternate_email)}")

            updated_fields = []

            if new_info.get('flight_details') and not claim.flight_details:
                claim.flight_details = new_info['flight_details']
                updated_fields.append('flight_details')
                log(f"Updated flight_details: {new_info['flight_details']}")

            if new_info.get('object_description') and not claim.object_description:
                claim.object_description = new_info['object_description']
                updated_fields.append('object_description')
                log(f"Updated object_description: {new_info['object_description']}")

            if new_info.get('phone') and not claim.phone:
                claim.phone = new_info['phone']
                updated_fields.append('phone')
                log(f"Updated phone: {new_info['phone']}")

            if new_info.get('alternate_email') and not claim.alternate_email:
                claim.alternate_email = new_info['alternate_email']
                updated_fields.append('alternate_email')
                log(f"Updated alternate_email: {new_info['alternate_email']}")

            # Save claim if any fields were updated
            if updated_fields:
                claim.save()
                log(f"Saved claim with updated fields: {', '.join(updated_fields)}")
            else:
                log(f"No fields were updated (all fields already have data or no new info found)")

            # Generate AI summary based on current ticket state
            ai_summary_parts = []
            if claim.flight_details:
                ai_summary_parts.append(f"Flight: {claim.flight_details}.")
            if claim.object_description:
                ai_summary_parts.append(f"Lost item: {claim.object_description}.")
            if claim.phone:
                ai_summary_parts.append(f"Phone: {claim.phone}.")
            if claim.alternate_email:
                ai_summary_parts.append(f"Alternate email: {claim.alternate_email}.")

            ai_summary = ' '.join(ai_summary_parts) if ai_summary_parts else 'No information available yet.'

            # Update the claim's AI summary
            claim.ai_summary = ai_summary
            claim.save(update_fields=['ai_summary'])  # Only update the ai_summary field
            log(f"Updated AI summary: {ai_summary}")

            # Create summary of what was found
            summary_parts = []
            if new_info.get('flight_details'):
                summary_parts.append(f"Found flight details: {new_info['flight_details']}")
            if new_info.get('object_description'):
                summary_parts.append(f"Found object description: {new_info['object_description']}")
            if new_info.get('phone'):
                summary_parts.append(f"Found phone: {new_info['phone']}")
            if new_info.get('alternate_email'):
                summary_parts.append(f"Found alternate email: {new_info['alternate_email']}")

            if summary_parts:
                summary = "; ".join(summary_parts)
            else:
                summary = "No new information found in Zendesk ticket update"

            # Create timeline entry
            from apps.claims.models import ClaimUpdateTimeline

            update_type = 'INFO_UPDATED' if updated_fields else 'LLM_ANALYSIS'
            log(f"Creating timeline entry with type: {update_type}")

            timeline_entry = ClaimUpdateTimeline.objects.create(
                claim=claim,
                zendesk_ticket_id=claim.zd_ticket_id,
                update_type=update_type,
                changes_summary=json.dumps({
                    'updated_fields': updated_fields,
                    'ticket_status': ticket_data.get('status'),
                    'new_info': new_info,
                }),
                llm_summary=summary,
            )
            log(f"Timeline entry created")

            log(f"=== UPDATE FROM ZENDESK COMPLETED ===")
            log(f"Summary: {summary}")
            log(f"Updated fields: {updated_fields}")

            # Save debug log to file
            with open(log_file, 'a') as f:
                f.write('\n'.join(debug_log) + '\n\n')

            return Response({
                'success': True,
                'summary': summary,
                'updated_fields': updated_fields,
                'timeline_entry_id': timeline_entry.id,
            })

        except Claim.DoesNotExist:
            log(f"Claim {claim_id} not found for Zendesk update")
            with open(log_file, 'a') as f:
                f.write('\n'.join(debug_log) + '\n\n')
            return Response(
                {'error': 'Claim not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            log(f"Error updating claim from Zendesk: {e}")
            import traceback
            log(f"Traceback: {traceback.format_exc()}")
            with open(log_file, 'a') as f:
                f.write('\n'.join(debug_log) + '\n\n')
            return Response(
                {'error': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
