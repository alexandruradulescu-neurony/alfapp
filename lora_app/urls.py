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
    path('agent/', include('apps.agent.urls')),
    # API endpoints
    path('api/claims/', include('apps.claims.urls')),
    path('api/communications/', include('apps.communications.urls')),
    path('api/integrations/', include('apps.integrations.urls')),
    path('api/payments/', include('apps.payments.urls')),
    path('api/agent/', include('apps.agent.api_urls')),
    path('api/services/', include(('apps.config.api.urls', 'api'), namespace='services')),
]

# Serve static files in development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    # Serve media files in development
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # In production (DEBUG=False), WhiteNoise serves STATIC files, but it does
    # NOT serve user-uploaded MEDIA. Evidence images are sensitive (tied to
    # claims), so serve them through a login-protected view — only authenticated
    # staff can fetch them. django.views.static.serve is adequate for an
    # internal, low-traffic tool; move media to object storage (R2/S3) if traffic
    # grows. Files live under MEDIA_ROOT, which should be a persistent disk
    # (e.g. a Railway Volume) so uploads survive redeploys.
    from django.contrib.auth.decorators import login_required
    from django.views.static import serve as media_serve
    from django.urls import re_path

    urlpatterns += [
        re_path(
            r'^media/(?P<path>.*)$',
            login_required(media_serve),
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]
