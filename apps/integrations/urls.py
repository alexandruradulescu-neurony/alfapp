from django.urls import path

from apps.integrations.views import ZendeskSidebarView, ZendeskTicketSyncView

urlpatterns = [
    path('zd/info/', ZendeskSidebarView.as_view(), name='zendesk-sidebar-info'),
    path('zd/sync/', ZendeskTicketSyncView.as_view(), name='zendesk-ticket-sync'),
]
