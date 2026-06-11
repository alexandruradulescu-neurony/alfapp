from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.claims.views import (
    ClaimViewSet,
    ClaimEvidenceViewSet,
    ClaimUpdateFromZendeskView,
    ClaimCheckEmailView,
)

router = DefaultRouter()
router.register(r'claims', ClaimViewSet, basename='claim')
router.register(r'evidence', ClaimEvidenceViewSet, basename='claim-evidence')

urlpatterns = [
    path('', include(router.urls)),
    path('<int:claim_id>/update-from-zendesk/', ClaimUpdateFromZendeskView.as_view(), name='claim-update-from-zendesk'),
    path('<int:claim_id>/check-email/', ClaimCheckEmailView.as_view(), name='claim-check-email'),
]
