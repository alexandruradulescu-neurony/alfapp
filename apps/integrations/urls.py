from django.urls import path

from apps.integrations.views import (
    ZendeskSidebarView,
    ZendeskTicketSyncView,
    ZendeskBriefingView,
    ZendeskChatView,
    ZendeskDraftView,
    ZendeskFlightLookupView,
    ZendeskEmailCheckView,
    ZendeskTicketEmailsView,
    ZendeskClientUpdatesView,
    FormFillStartView,
    FormFillStatusView,
    FormFillSubmitView,
    FormFillCancelView,
    RefundWebhookView,
    ZendeskClaimWebhookView,
    ZendeskClientReplyWebhookView,
)

urlpatterns = [
    path('zd/info/', ZendeskSidebarView.as_view(), name='zendesk-sidebar-info'),
    path('zd/sync/', ZendeskTicketSyncView.as_view(), name='zendesk-ticket-sync'),
    path('zd/briefing/', ZendeskBriefingView.as_view(), name='zendesk-sidebar-briefing'),
    path('zd/chat/', ZendeskChatView.as_view(), name='zendesk-sidebar-chat'),
    path('zd/draft/', ZendeskDraftView.as_view(), name='zendesk-sidebar-draft'),
    path('zd/flight-lookup/', ZendeskFlightLookupView.as_view(), name='zendesk-flight-lookup'),
    path('zd/email-check/', ZendeskEmailCheckView.as_view(), name='zendesk-email-check'),
    path('zd/emails/', ZendeskTicketEmailsView.as_view(), name='zendesk-ticket-emails'),
    path('zd/updates/', ZendeskClientUpdatesView.as_view(), name='zendesk-client-updates'),
    path('zd/form-fill/start/', FormFillStartView.as_view(), name='zd-form-fill-start'),
    path('zd/form-fill/status/', FormFillStatusView.as_view(), name='zd-form-fill-status'),
    path('zd/form-fill/submit/', FormFillSubmitView.as_view(), name='zd-form-fill-submit'),
    path('zd/form-fill/cancel/', FormFillCancelView.as_view(), name='zd-form-fill-cancel'),
    path('zd/refund-webhook/', RefundWebhookView.as_view(), name='zendesk-refund-webhook'),
    path('zd/claim-webhook/', ZendeskClaimWebhookView.as_view(), name='zendesk-claim-webhook'),
    path('zd/client-reply-webhook/', ZendeskClientReplyWebhookView.as_view(), name='zendesk-client-reply-webhook'),
]
