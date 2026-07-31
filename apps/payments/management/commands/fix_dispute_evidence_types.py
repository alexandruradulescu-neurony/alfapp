"""Correct saved dispute DRAFT evidence types to a label PayPal accepts.

LORA used to stamp every draft PROOF_OF_FULFILLMENT, which PayPal rejects for
"not as described" disputes (EVIDENCE_TYPE_IS_NOT_ALLOWED). This realigns the
already-prepared drafts on open disputes to a label PayPal accepts.

    python manage.py fix_dispute_evidence_types --dry-run   # show what would change
    python manage.py fix_dispute_evidence_types             # apply
"""
from django.core.management.base import BaseCommand

from apps.payments.paypal_disputes_service import fix_dispute_evidence_types


class Command(BaseCommand):
    help = ("Realign saved dispute DRAFT evidence types to a label PayPal accepts "
            "for each dispute (fixes the old blanket PROOF_OF_FULFILLMENT).")

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Only report what would change; persist nothing.')

    def handle(self, *args, **opts):
        result = fix_dispute_evidence_types(dry_run=opts['dry_run'])
        label = 'DRY RUN' if opts['dry_run'] else 'LIVE'
        self.stdout.write(self.style.SUCCESS(
            f"fix_dispute_evidence_types ({label}): "
            f"checked={result['checked']} updated={result['updated']} "
            f"changes={result['changes']}"))
