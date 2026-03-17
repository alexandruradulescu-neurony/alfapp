from rest_framework import serializers
from apps.config.models import ServiceStatus


class ServiceStatusSerializer(serializers.ModelSerializer):
    """Serializer for ServiceStatus model."""
    
    service_name = serializers.CharField(source='get_service_display', read_only=True)
    status_name = serializers.CharField(source='get_status_display', read_only=True)
    status_color = serializers.CharField(source='get_status_color', read_only=True)
    
    class Meta:
        model = ServiceStatus
        fields = [
            'service',
            'service_name',
            'status',
            'status_name',
            'status_color',
            'is_enabled',
            'last_checked',
            'last_error',
            'metadata'
        ]
        read_only_fields = ['last_checked', 'metadata']


class ServiceTestResultSerializer(serializers.Serializer):
    """Serializer for service test results."""
    
    success = serializers.BooleanField()
    status = serializers.CharField()
    message = serializers.CharField()
    service = serializers.CharField(required=False)


class SchedulerInfoSerializer(serializers.Serializer):
    """Serializer for scheduler information."""
    
    success = serializers.BooleanField()
    running = serializers.BooleanField()
    enabled = serializers.BooleanField()
    status = serializers.CharField()
    jobs = serializers.ListField(child=serializers.DictField())
    message = serializers.CharField(required=False)


class ToggleSerializer(serializers.Serializer):
    """Serializer for toggle requests."""
    
    enabled = serializers.BooleanField(required=True)
