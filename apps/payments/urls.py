from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.payments.views import (
    PayPalWebhookView,
    PayPalDisputeWebhookView,
    ProofOfWorkPDFView,
    RefundViewSet,
)

router = DefaultRouter()
router.register(r'refunds', RefundViewSet, basename='refund')

urlpatterns = [
    # API endpoints
    path('paypal/webhook/', PayPalWebhookView.as_view(), name='paypal-webhook'),
    path('paypal/dispute-webhook/', PayPalDisputeWebhookView.as_view(), name='paypal-dispute-webhook'),
    path('proof-of-work/<int:claim_id>/', ProofOfWorkPDFView.as_view(), name='proof-of-work-pdf'),
    # Refund API
    path('', include(router.urls)),
]
