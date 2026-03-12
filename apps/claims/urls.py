from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.claims.views import ClaimViewSet, ClaimEvidenceViewSet

router = DefaultRouter()
router.register(r'claims', ClaimViewSet, basename='claim')
router.register(r'evidence', ClaimEvidenceViewSet, basename='claim-evidence')

urlpatterns = [
    path('', include(router.urls)),
]
