"""Backfill Claim.submitted_at from each claim's WooCommerce order payment date.

submitted_at is the TRUE claim date (when the cart became a paid claim). Without it,
reports group claims by created_at — the LORA import date — so back-imported old
tickets all pile onto the import day. This pulls the real date from WooCommerce.

    python manage.py backfill_claim_dates            # only claims missing submitted_at
    python manage.py backfill_claim_dates --all       # re-fetch every claim with an order
    python manage.py backfill_claim_dates --limit 50
"""
from django.core.management.base import BaseCommand

from apps.claims.services import backfill_claim_dates


class Command(BaseCommand):
    help = ("Set Claim.submitted_at from each claim's WooCommerce order payment date "
            "(the true claim date) so reports stop clustering imports on the import day.")

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true',
                            help='Re-fetch every claim with an order, not only those missing a date.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Process at most N claims (newest first).')

    def handle(self, *args, **opts):
        result = backfill_claim_dates(only_missing=not opts['all'], limit=opts['limit'])
        self.stdout.write(self.style.SUCCESS(f"backfill_claim_dates: {result}"))
