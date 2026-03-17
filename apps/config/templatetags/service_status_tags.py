from django import template
from apps.config.models import ServiceStatus

register = template.Library()


@register.simple_tag
def get_service_status(service_name):
    """Get service status by name."""
    try:
        return ServiceStatus.objects.get(service=service_name)
    except ServiceStatus.DoesNotExist:
        return None
