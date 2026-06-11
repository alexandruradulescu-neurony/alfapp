from django.urls import path

from apps.integrations.views import (
    ZendeskSidebarView,
    ZendeskTicketSyncView,
    ZendeskBriefingView,
    ZendeskChatView,
    ZendeskDraftView,
    ZendeskFlightLookupView,
    RefundWebhookView,
    ZendeskClaimWebhookView,
)

urlpatterns = [
    path('zd/info/', ZendeskSidebarView.as_view(), name='zendesk-sidebar-info'),
    path('zd/sync/', ZendeskTicketSyncView.as_view(), name='zendesk-ticket-sync'),
    path('zd/briefing/', ZendeskBriefingView.as_view(), name='zendesk-sidebar-briefing'),
    path('zd/chat/', ZendeskChatView.as_view(), name='zendesk-sidebar-chat'),
    path('zd/draft/', ZendeskDraftView.as_view(), name='zendesk-sidebar-draft'),
    path('zd/flight-lookup/', ZendeskFlightLookupView.as_view(), name='zendesk-flight-lookup'),
    path('zd/refund-webhook/', RefundWebhookView.as_view(), name='zendesk-refund-webhook'),
    path('zd/claim-webhook/', ZendeskClaimWebhookView.as_view(), name='zendesk-claim-webhook'),
]
