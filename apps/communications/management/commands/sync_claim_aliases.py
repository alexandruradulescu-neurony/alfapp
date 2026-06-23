"""Sync each claim's submission alias from its Zendesk ticket ("Email used for
submissions" field) into LORA, so inbound institution emails can be matched LOCALLY.
Zendesk's search does not reliably match email-valued custom fields, so the sweep relies
on this local index instead.

    python manage.py sync_claim_aliases                 # sync all claims with a ticket
    python manage.py sync_claim_aliases --only-missing  # only those with no cached alias
    python manage.py sync_claim_aliases --limit 50
"""
from django.core.management.base import BaseCommand

from apps.integrations.services import sync_claim_aliases


class Command(BaseCommand):
    help = ("Populate Claim.email_alias from each claim's Zendesk ticket so inbound "
            "emails match locally (Zendesk search is unreliable for email fields).")

    def add_arguments(self, parser):
        parser.add_argument('--only-missing', action='store_true',
                            help='Only sync claims that have no cached alias yet.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Sync at most N claims (newest first).')

    def handle(self, *args, **opts):
        result = sync_claim_aliases(only_missing=opts['only_missing'], limit=opts['limit'])
        self.stdout.write(self.style.SUCCESS(f"sync_claim_aliases: {result}"))
