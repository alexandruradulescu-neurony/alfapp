"""Send the evidence-report PDF as a PayPal follow-up on disputes whose earlier
replies went out WITHOUT it (the attach tick silently defaulted off — fixed in
PR #114; this command repairs the already-sent cases).

    python manage.py send_missing_dispute_reports --dry-run   # list who qualifies
    python manage.py send_missing_dispute_reports             # actually send
"""
from django.core.management.base import BaseCommand

from apps.payments.paypal_disputes_service import send_missing_dispute_reports


class Command(BaseCommand):
    help = ("Backfill: attach + send the evidence report as a follow-up on disputes "
            "whose earlier PayPal replies went out without it.")

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Only count/list the qualifying disputes; send nothing.')

    def handle(self, *args, **opts):
        result = send_missing_dispute_reports(dry_run=opts['dry_run'])
        label = 'DRY RUN' if opts['dry_run'] else 'LIVE'
        self.stdout.write(self.style.SUCCESS(
            f"send_missing_dispute_reports ({label}): "
            f"candidates={result['candidates']} disputes={result['disputes']} "
            f"sent={result['sent']} failed={result['failed']} "
            f"failed_disputes={result['failed_disputes']} "
            f"skipped_draft={result['skipped_draft']}"))
