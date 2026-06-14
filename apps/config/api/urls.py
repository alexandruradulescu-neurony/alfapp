from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ServiceStatusViewSet,
    test_connection,
    toggle_service,
    toggle_setting_flag,
)

router = DefaultRouter()
router.register(r'status', ServiceStatusViewSet, basename='status')

app_name = 'services'

urlpatterns = [
    path('', include(router.urls)),
    path('settings-flag/', toggle_setting_flag, name='settings-flag'),
    path('<str:service>/test/', test_connection, name='test-connection'),
    path('<str:service>/toggle/', toggle_service, name='toggle'),
]
