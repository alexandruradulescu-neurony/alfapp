"""
URL configuration for LORA (Lost Object Recovery Automation) project.
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

# Custom error handlers
handler404 = 'lora_app.views.custom_404'
handler500 = 'lora_app.views.custom_500'

urlpatterns = [
    path('admin/', admin.site.urls),
    # Frontend views
    path('', include('apps.users.urls')),
    # API endpoints
    path('api/claims/', include('apps.claims.urls')),
    path('api/communications/', include('apps.communications.urls')),
    path('api/integrations/', include('apps.integrations.urls')),
    path('api/payments/', include('apps.payments.urls')),
]

# Serve static files in development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    # Serve media files in development
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
