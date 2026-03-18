from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ServiceStatusViewSet,
    test_connection,
    toggle_service,
    scheduler_start,
    scheduler_stop,
    scheduler_toggle,
    scheduler_info
)

router = DefaultRouter()
router.register(r'status', ServiceStatusViewSet, basename='status')

app_name = 'services'

urlpatterns = [
    path('', include(router.urls)),
    path('<str:service>/test/', test_connection, name='test-connection'),
    path('<str:service>/toggle/', toggle_service, name='toggle'),
    path('scheduler/start/', scheduler_start, name='scheduler-start'),
    path('scheduler/stop/', scheduler_stop, name='scheduler-stop'),
    path('scheduler/toggle/', scheduler_toggle, name='scheduler-toggle'),
    path('scheduler/info/', scheduler_info, name='scheduler-info'),
]
