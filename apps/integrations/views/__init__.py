"""Zendesk integration views for LORA — package hub.

Every endpoint class lives in a focused module; they are re-exported here so
`from apps.integrations.views import X` and urls.py keep working unchanged.
"""

from apps.integrations.views.auth import ZendeskSidebarAuth
from apps.integrations.views.sidebar import ZendeskSidebarView
from apps.integrations.views.assist import (
    ZendeskBriefingView,
    ZendeskDraftView,
    ZendeskChatView,
)
from apps.integrations.views.sync import ZendeskTicketSyncView
from apps.integrations.views.flight import ZendeskFlightLookupView
from apps.integrations.views.webhooks import (
    RefundWebhookView, ZendeskClaimWebhookView, ZendeskClientReplyWebhookView,
)
from apps.integrations.views.email import ZendeskEmailCheckView, ZendeskTicketEmailsView
from apps.integrations.views.client_updates import ZendeskClientUpdatesView
from apps.integrations.views.form_fill import (
    FormFillStartView, FormFillStatusView, FormFillSubmitView, FormFillCancelView,
    FormFillAttachmentsView, FormFillUploadView, FormFillWebhookView,
)

__all__ = [
    'ZendeskSidebarAuth',
    'ZendeskSidebarView',
    'ZendeskBriefingView',
    'ZendeskDraftView',
    'ZendeskChatView',
    'ZendeskTicketSyncView',
    'ZendeskFlightLookupView',
    'RefundWebhookView',
    'ZendeskClaimWebhookView',
    'ZendeskClientReplyWebhookView',
    'ZendeskEmailCheckView',
    'ZendeskTicketEmailsView',
    'ZendeskClientUpdatesView',
    'FormFillStartView',
    'FormFillStatusView',
    'FormFillSubmitView',
    'FormFillCancelView',
    'FormFillAttachmentsView',
    'FormFillUploadView',
    'FormFillWebhookView',
]
