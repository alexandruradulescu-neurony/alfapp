"""Read endpoints for the sidebar Email tab: trigger a mailbox check for a
ticket, and list the ticket's stored email history.

Split out of the integrations views package (untangling refactor); both classes
moved verbatim.
"""

import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from apps.claims.models import Claim
from apps.communications.models import EmailLog
from apps.integrations.services import fetch_zendesk_ticket, get_ticket_email_alias
from apps.integrations.views.auth import ZendeskSidebarAuth

logger = logging.getLogger(__name__)

# Maximum email rows returned by the read-only ticket-emails list endpoint.
EMAILS_PAGE_SIZE = 50


class ZendeskEmailCheckView(APIView):
    """POST /api/integrations/zd/email-check/
    Body: {ticket_id}

    The sidebar Email tab's button: checks the shared mailbox for new mail
    addressed to THIS ticket's email alias only (unread, last 2 days, never
    processed before). New mail gets AI categorization, an EmailLog row, an
    internal note on the ticket and additive ai_* tags. The rest of the
    inbox is untouched.

    Claim-first, fields-fallback: a linked claim caches the alias; without
    one the alias is read from the ticket's Email Alias custom field.
    Auth: ZendeskSidebarAuth."""

    permission_classes = [AllowAny]

    def post(self, request):
        from apps.communications.services import (
            EmailNotConfigured, InvalidAlias, check_email_for_ticket)

        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='email-check')
        if auth_error:
            return auth_error

        ticket_id = str(request.data.get('ticket_id', '')).strip()
        if not ticket_id:
            return Response({'error_message': 'No ticket id received.'},
                            status=status.HTTP_200_OK)

        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()
        alias = claim.email_alias if claim else ''
        if not alias:
            ticket_data = fetch_zendesk_ticket(ticket_id)
            if ticket_data is None:
                return Response(
                    {'error_message': "Couldn't read this ticket's fields from Zendesk."},
                    status=status.HTTP_200_OK)
            alias = get_ticket_email_alias(ticket_data)
            if not alias:
                return Response(
                    {'error_message': 'This ticket has no email alias field — '
                                      'there is no address to check mail for.'},
                    status=status.HTTP_200_OK)
            if claim:
                claim.email_alias = alias
                claim.save(update_fields=['email_alias', 'updated_at'])

        try:
            results = check_email_for_ticket(ticket_id, claim, alias)
        except EmailNotConfigured:
            return Response(
                {'error': 'Mailbox (IMAP) credentials are not configured in System settings.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except InvalidAlias:
            return Response(
                {'error_message': "The ticket's email alias doesn't look like an "
                                  "email address — fix the Email Alias field."},
                status=status.HTTP_200_OK)
        except Exception as e:
            logger.error("Email check failed for ticket %s: %s", ticket_id, e, exc_info=True)
            return Response({'error': 'Could not reach the mailbox. Try again.'},
                            status=status.HTTP_502_BAD_GATEWAY)

        new_count = len(results['processed'])
        subject = f"claim #{claim.id}" if claim else f"claimless ticket {ticket_id}"
        logger.info("Email check for %s (%s): %s new, %s already processed, tags=%s",
                    subject, alias, new_count, results['already_processed'],
                    results['tags_added'])
        return Response({'message': f"{new_count} new email(s) processed",
                         'claimless': claim is None, **results},
                        status=status.HTTP_200_OK)


class ZendeskTicketEmailsView(APIView):
    """POST /api/integrations/zd/emails/  Body: {ticket_id}

    Read-only window onto the SAME stored EmailLog rows the LORA app shows —
    so the sidebar's Email tab lists the ticket's real email history, not just
    the last check's results. No doubling: one store, two views.
    Auth: ZendeskSidebarAuth."""

    permission_classes = [AllowAny]

    def post(self, request):
        auth_error = ZendeskSidebarAuth.reject_if_unauthenticated(request, context='emails-list')
        if auth_error:
            return auth_error

        ticket_id = str(request.data.get('ticket_id', '')).strip()
        if not ticket_id:
            return Response({'emails': []}, status=status.HTTP_200_OK)

        # Match the claim's emails when linked, else by ticket id (claimless).
        claim = Claim.objects.filter(zd_ticket_id=ticket_id).first()
        qs = (claim.emails.all() if claim
              else EmailLog.objects.filter(zd_ticket_id=ticket_id))
        emails = [{
            'id': e.id,
            'subject': e.subject,
            'from_email': e.from_email,
            'category': e.get_category_display(),
            'summary': e.ai_summary,
            'action_required': e.action_required,
            'auto_resolved': e.auto_resolved,
            'received_at': e.received_at.isoformat() if e.received_at else None,
        } for e in qs.order_by('-received_at')[:EMAILS_PAGE_SIZE]]
        return Response({'emails': emails, 'claimless': claim is None},
                        status=status.HTTP_200_OK)
