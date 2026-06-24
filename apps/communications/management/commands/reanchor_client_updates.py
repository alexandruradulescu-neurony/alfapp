"""Re-anchor existing client-update reminders to each claim's TRUE date (submitted_at).

Reminders for back-imported old claims were scheduled off the import date, so they'd
fire weeks late with stale content. This recomputes each open reminder from the real
WooCommerce-order date: an entirely-past cadence is cancelled (service window closed),
a still-future reminder is rescheduled, same-day claims are left alone.

    python manage.py reanchor_client_updates --dry-run     # preview counts, change nothing
    python manage.py reanchor_client_updates               # apply
"""
from django.core.management.base import BaseCommand

from apps.communications.client_updates import reanchor_client_updates


class Command(BaseCommand):
    help = ("Re-anchor open client-update reminders to the claim's true date (submitted_at); "
            "cancel cadences whose window has fully passed.")

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without writing.')

    def handle(self, *args, **opts):
        result = reanchor_client_updates(dry_run=opts['dry_run'])
        label = 'reanchor_client_updates (DRY RUN)' if opts['dry_run'] else 'reanchor_client_updates'
        self.stdout.write(self.style.SUCCESS(f"{label}: {result}"))
