"""Backfill existing emails: recover empty bodies (re-fetch from the mailbox by
Message-ID) and re-categorize the suspect set (empty-body + GENERAL_CORRESPONDENCE +
UNKNOWN) with the current, shipping-aware categorizer, then re-apply Zendesk tags.

    python manage.py reprocess_emails --dry-run          # counts only, no changes/AI
    python manage.py reprocess_emails                    # full run
    python manage.py reprocess_emails --limit 50         # process at most 50
    python manage.py reprocess_emails --claim 215        # restrict to one claim
"""
from django.core.management.base import BaseCommand

from apps.communications.services import reprocess_email_logs


class Command(BaseCommand):
    help = ("Recover empty email bodies and re-categorize suspect emails "
            "(incl. the new shipping category), re-tagging Zendesk. Idempotent.")

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report counts only; no IMAP, no AI, no writes.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Process at most N emails (newest first).')
        parser.add_argument('--claim', type=int, default=None,
                            help='Restrict to a single claim id.')
        parser.add_argument('--scan', type=int, default=500,
                            help='How many of the most recent mailbox messages to scan '
                                 'when matching empty bodies by Message-ID (default 500). '
                                 'Bump this if the logs show seq=None misses.')

    def handle(self, *args, **opts):
        result = reprocess_email_logs(
            dry_run=opts['dry_run'], limit=opts['limit'], claim_id=opts['claim'],
            scan=opts['scan'])
        self.stdout.write(self.style.SUCCESS(f"reprocess_emails: {result}"))
