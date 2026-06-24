"""Retroactively apply AI category tags to Zendesk tickets from their EmailLogs.

The global email sweep historically categorized inbound mail (in EmailLog) without
ever pushing the ai_* tags to Zendesk — only the manual per-ticket button did. So
many tickets are missing tags like ai_object_found / ai_shipping_information /
ai_attention_needed. This recomputes each ticket's tags from the union of its
EmailLogs and ADDS them (additive on this instance — workflow tags are untouched).

    python manage.py retag_tickets --dry-run     # show what WOULD be tagged
    python manage.py retag_tickets               # apply
    python manage.py retag_tickets --limit 20
"""
from collections import defaultdict

from django.core.management.base import BaseCommand

from apps.communications.models import EmailLog
from apps.communications.services import _ai_tags_for, retag_tickets_from_email_logs


class Command(BaseCommand):
    help = ("Apply AI category tags to Zendesk tickets from their EmailLogs "
            "(additive; fixes tickets the sweep categorized but never tagged).")

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='List the tags that WOULD be applied per ticket; change nothing.')
        parser.add_argument('--limit', type=int, default=None,
                            help='Tag at most N tickets.')

    def handle(self, *args, **opts):
        if opts['dry_run']:
            by_ticket = defaultdict(set)
            for tid, cat, act in (EmailLog.objects.exclude(zd_ticket_id__in=['', None])
                                   .values_list('zd_ticket_id', 'category', 'action_required')):
                by_ticket[str(tid)].update(_ai_tags_for(cat, act))
            rows = sorted((tid, sorted(tags)) for tid, tags in by_ticket.items() if tags)
            for tid, tags in rows[:opts['limit'] or len(rows)]:
                self.stdout.write(f"  {tid}: {tags}")
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — {len(rows)} ticket(s) would be tagged, nothing changed."))
            return
        result = retag_tickets_from_email_logs(limit=opts['limit'])
        self.stdout.write(self.style.SUCCESS(f"retag_tickets: {result}"))
