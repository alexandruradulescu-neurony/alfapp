"""Re-route EmailLog rows that were swept as 'orphans' (no Zendesk ticket matched,
usually because alias matching needed a configured domain) to their tickets, using
the now-domain-agnostic matching and the analysis already stored — no IMAP re-fetch,
no new AI calls. Genuine non-case mail stays orphaned. Run once after the matching fix."""
import logging

from django.core.management.base import BaseCommand

from apps.communications.services import (
    find_zendesk_ticket_for_email, recover_orphan_emails)
from apps.communications.models import EmailLog

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-match orphaned EmailLogs to Zendesk tickets and post the stored analysis."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be re-routed without changing anything.')

    def handle(self, *args, **opts):
        dry = opts['dry_run']

        # Print per-email lines (matching the original output style) before
        # delegating the actual work to the shared recover_orphan_emails helper.
        from apps.communications.models import EmailLog
        from apps.communications.services import find_zendesk_ticket_for_email
        import email as email_lib
        orphans = EmailLog.objects.filter(zd_ticket_id__in=['', None]).exclude(raw_headers='')
        for el in orphans:
            try:
                msg = email_lib.message_from_string(el.raw_headers)
            except Exception:
                continue
            ticket, alias = find_zendesk_ticket_for_email(msg)
            if not ticket:
                continue
            zd_ticket_id = str(ticket.get('id', ''))
            self.stdout.write(f"EmailLog #{el.id} -> ticket {zd_ticket_id} (alias {alias})")

        result = recover_orphan_emails(dry_run=dry)
        matched = result['matched']
        self.stdout.write(self.style.SUCCESS(
            f"{'Would re-route' if dry else 'Re-routed'} {matched} orphan email(s)."))
