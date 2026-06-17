"""Zendesk client-updates endpoint: the sidebar timeline of the initial client
message + scheduled follow-ups, with prepare/send/skip/start actions. Split out
of the integrations views package; class moved verbatim. (Its business logic is
a future candidate for extraction into the communications service layer.)"""

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from apps.claims.models import Claim
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
            message = self._act(request, claim, action)

        return Response({**self._timeline(claim), 'message': message}, status=status.HTTP_200_OK)

    def _act(self, request, claim, action) -> str:
        from django.utils import timezone
        from apps.communications import client_updates as cu

        if action == 'start':
            return ('Client updates started — the initial draft is ready and follow-ups scheduled.'
                    if cu.start_client_updates(claim) else 'Updates already started for this claim.')

        kind = (request.data.get('kind') or '').strip()
        body = (request.data.get('body') or '').strip()

        if kind == 'initial':
            if action == 'prepare':
                from apps.communications.client_report import build_client_update_message
                claim.client_report_draft = build_client_update_message(claim, polish=True)
                claim.save(update_fields=['client_report_draft', 'updated_at'])
                return 'Initial update regenerated.'
            if action == 'send':
                if claim.client_report_sent_at:
                    return 'The initial update was already sent.'
                if not body or not claim.zd_ticket_id:
                    return 'Nothing to send.'
                from apps.integrations.services import post_zendesk_comment
                if post_zendesk_comment(claim.zd_ticket_id, body, is_internal=False) is None:
                    return 'Could not post the reply to Zendesk.'
                claim.client_report_draft = body
                claim.client_report_sent_at = timezone.now()
                claim.save(update_fields=['client_report_draft', 'client_report_sent_at', 'updated_at'])
                return 'Initial update sent as a public reply.'
            return ''

        # follow-up
        update = claim.follow_up_updates.filter(id=request.data.get('id')).first()
        if not update:
            return 'Update not found.'
        if action == 'prepare':
            cu.prepare_follow_up(update)
            return f'{update.label} update drafted.'
        if action == 'skip':
            cu.skip_follow_up(update)
            return f'{update.label} update skipped.'
        if action == 'send':
            if update.state == 'SENT':
                return 'That update was already sent.'
            if cu.send_follow_up(update, body):
                return f'{update.label} update sent as a public reply.'
            return 'Could not post the reply to Zendesk.'
        return ''

    def _timeline(self, claim) -> dict:
        from django.utils import timezone
        now = timezone.now()
        items = []
        if claim.client_report_draft or claim.client_report_sent_at:
            items.append({
                'kind': 'initial', 'label': 'Initial update', 'due_label': 'On submission',
                'state': 'sent' if claim.client_report_sent_at else 'drafted',
                'body': claim.client_report_draft,
                'has_news': True,
                'sent_at': claim.client_report_sent_at.isoformat() if claim.client_report_sent_at else None,
                'can_send': bool(claim.zd_ticket_id),
            })
        for fu in claim.follow_up_updates.all().order_by('due_at'):
            items.append({
                'kind': 'followup', 'id': fu.id, 'label': fu.label,
                'milestone': fu.milestone, 'state': fu.state.lower(),
                'due_at': fu.due_at.isoformat(),
                'is_due': fu.state == 'SCHEDULED' and fu.due_at <= now,
                'has_news': fu.has_news, 'body': fu.draft_body,
                'sent_at': fu.sent_at.isoformat() if fu.sent_at else None,
                'can_send': bool(claim.zd_ticket_id),
            })
        return {'claim': True, 'alf_id': claim.alf_claim_id or '', 'items': items}
