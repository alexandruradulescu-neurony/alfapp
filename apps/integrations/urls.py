from django.urls import path

from apps.integrations.views import (
    ZendeskSidebarView,
    ZendeskTicketSyncView,
    RefundWebhookView,
    ZendeskStatusWebhookView,
)

urlpatterns = [
    path('zd/info/', ZendeskSidebarView.as_view(), name='zendesk-sidebar-info'),
    path('zd/sync/', ZendeskTicketSyncView.as_view(), name='zendesk-ticket-sync'),
    path('zd/refund-webhook/', RefundWebhookView.as_view(), name='zendesk-refund-webhook'),
    path('zd/status-webhook/', ZendeskStatusWebhookView.as_view(), name='zendesk-status-webhook'),
]
