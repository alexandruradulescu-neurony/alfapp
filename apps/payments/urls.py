from django.urls import path

from apps.payments.views import PayPalWebhookView, ProofOfWorkPDFView, DisputeScreenshotCaptureView

urlpatterns = [
    # API endpoints
    path('paypal/webhook/', PayPalWebhookView.as_view(), name='paypal-webhook'),
    path('proof-of-work/<int:claim_id>/', ProofOfWorkPDFView.as_view(), name='proof-of-work-pdf'),
    path('disputes/<int:dispute_id>/capture-screenshot/', DisputeScreenshotCaptureView.as_view(), name='dispute-screenshot-capture'),
]
