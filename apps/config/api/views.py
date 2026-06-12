from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from apps.config.models import ServiceStatus
from apps.config.api.serializers import (
    ServiceStatusSerializer,
    ServiceTestResultSerializer,
    SchedulerInfoSerializer,
    ToggleSerializer
)
from apps.config.services.connection_tester import ConnectionTester
from apps.config.services.scheduler_controller import SchedulerController


class ServiceStatusViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing service status."""
    
    queryset = ServiceStatus.objects.all()
    serializer_class = ServiceStatusSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = 'service'  # Use service field instead of id
    
    def get_object(self):
        """Get object by service field."""
        queryset = self.get_queryset()
        service = self.kwargs.get('service')
        obj = get_object_or_404(queryset, service=service)
        self.check_object_permissions(self.request, obj)
        return obj
    
    def list(self, request, *args, **kwargs):
        """List all service statuses."""
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)
        return Response({
            'services': serializer.data
        })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def test_connection(request, service):
    """Test connection for a specific service."""
    tester = ConnectionTester()
    
    test_methods = {
        'AI': tester.test_ai,
        'IMAP': tester.test_imap,
        'ZENDESK': tester.test_zendesk,
        'PAYPAL': tester.test_paypal,
        'WOOCOMMERCE': tester.test_woocommerce,
        'SCHEDULER': tester.get_scheduler_status,
        'SCREENSHOT': tester.get_screenshot_status,
    }
    
    if service not in test_methods:
        return Response(
            {'error': f'Unknown service: {service}'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    result = test_methods[service]()
    serializer = ServiceTestResultSerializer(result)
    return Response(serializer.data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def toggle_service(request, service):
    """Toggle enabled state for a service."""
    if service == 'SCHEDULER':
        serializer = ToggleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        controller = SchedulerController()
        result = controller.toggle_enabled(serializer.validated_data['enabled'])
        return Response(result)
    
    # For other services, just update the enabled flag
    status_obj = get_object_or_404(ServiceStatus, service=service)
    serializer = ToggleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    status_obj.is_enabled = serializer.validated_data['enabled']
    status_obj.save()
    
    return Response({
        'success': True,
        'service': service,
        'is_enabled': status_obj.is_enabled,
        'message': f'Service {service} {"enabled" if status_obj.is_enabled else "disabled"}'
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def scheduler_start(request):
    """Start the email scheduler."""
    controller = SchedulerController()
    result = controller.start()
    return Response(result)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def scheduler_stop(request):
    """Stop the email scheduler."""
    controller = SchedulerController()
    result = controller.stop()
    return Response(result)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def scheduler_toggle(request):
    """Toggle scheduler enabled state."""
    serializer = ToggleSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    controller = SchedulerController()
    result = controller.toggle_enabled(serializer.validated_data['enabled'])
    return Response(result)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def scheduler_info(request):
    """Get scheduler information."""
    controller = SchedulerController()
    result = controller.get_info()
    serializer = SchedulerInfoSerializer(result)
    return Response(serializer.data)
