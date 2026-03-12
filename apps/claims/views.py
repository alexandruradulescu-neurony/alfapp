import logging

from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter

from apps.claims.models import Claim, ClaimEvidence
from apps.claims.serializers import ClaimSerializer, ClaimDetailSerializer, ClaimEvidenceSerializer

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


class IsManager(permissions.BasePermission):
    """
    Custom permission to allow only MANAGER users.
    """

    def has_permission(self, request, view):
        return request.user.is_authenticated and getattr(request.user, 'role', None) == 'MANAGER'

    def has_object_permission(self, request, view, obj):
        return request.user.is_authenticated and getattr(request.user, 'role', None) == 'MANAGER'


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
        Optimized to prevent N+1 queries with select_related and prefetch_related.
        MANAGERs see all claims, AGENTs see all claims (can be modified for multi-tenant).
        """
        queryset = super().get_queryset()
        user = self.request.user
        
        # Optimize queries: select_related for FK, prefetch_related for reverse FK
        queryset = queryset.select_related('assigned_to').prefetch_related('evidence', 'emails')
        
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
