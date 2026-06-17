"""Zendesk client-updates endpoint: the sidebar timeline of the initial client
message + scheduled follow-ups, with prepare/send/skip/start actions. Thin
mapper over apps.communications.client_updates — it parses the request and maps
the result back to a Response; all routing/guards/timeline logic live in the
communications service layer."""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from apps.claims.models import Claim
from apps.communications.client_updates import (
    apply_update_action,
    build_client_update_timeline,
)
from apps.integrations.views.auth import ZendeskSidebarAuth


class ZendeskClientUpdatesView(APIView):
    """POST /api/integrations/zd/updates/  Body: {ticket_id, action, kind, id, body}

    The Zendesk-side surface for client progress updates: a timeline of the
    initial "what we did" message + the day-2/5/11/21 follow-ups, with prepare/
    send/skip actions. Reads/writes the SAME LORA data the claim page uses (one
    store, two views). Auth: ZendeskSidebarAuth. Draft-for-approval — send always
    posts a PUBLIC reply only when the agent triggers it."""

    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='updates')
        if auth_error:
            return auth_error

        ticket_id = str(request.data.get('ticket_id', '')).strip()
        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first() if ticket_id else None
        if not claim:
            return Response({'claim': False, 'items': []}, status=status.HTTP_200_OK)

        action = (request.data.get('action') or 'list').strip()
        message = ''
        if action in ('send', 'prepare', 'skip', 'start'):
            message = apply_update_action(
                claim,
                action=action,
                kind=(request.data.get('kind') or '').strip(),
                body=(request.data.get('body') or '').strip(),
                update_id=request.data.get('id'),
            )

        return Response({**build_client_update_timeline(claim), 'message': message},
                        status=status.HTTP_200_OK)
