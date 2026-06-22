"""Re-route EmailLog rows that were swept as 'orphans' (no Zendesk ticket matched,
usually because alias matching needed a configured domain) to their tickets, using
the now-domain-agnostic matching and the analysis already stored — no IMAP re-fetch,
no new AI calls. Genuine non-case mail stays orphaned. Run once after the matching fix."""
import email as email_lib
import logging

from django.core.management.base import BaseCommand

from apps.communications.models import EmailLog
from apps.communications.services import (
    find_zendesk_ticket_for_email, post_ai_summary_to_zendesk, _ai_tags_for)
from apps.config.models import SystemSettings

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-match orphaned EmailLogs to Zendesk tickets and post the stored analysis."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be re-routed without changing anything.')

    def handle(self, *args, **opts):
        from apps.claims.models import Claim
        from apps.integrations.services import add_zendesk_ticket_tags, import_claim_from_zendesk_ticket
        dry = opts['dry_run']
        orphans = EmailLog.objects.filter(zd_ticket_id__in=['', None]).exclude(raw_headers='')
        matched = 0
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
            matched += 1
            if dry:
                continue
            claim = Claim.objects.filter(zd_ticket_id=zd_ticket_id).first()
            if claim is None and getattr(SystemSettings.get_instance(), 'import_claims_from_email', False):
                imported, _created = import_claim_from_zendesk_ticket(zd_ticket_id)
                claim = imported or claim
            el.zd_ticket_id = zd_ticket_id
            el.claim = claim
            el.alias_matched = alias
            el.save(update_fields=['zd_ticket_id', 'claim', 'alias_matched'])
            parsed = {'category': el.category, 'summary': el.ai_summary,
                      'action_required': el.action_required, 'auto_resolvable': el.auto_resolved}
            post_ai_summary_to_zendesk(zd_ticket_id=zd_ticket_id, parsed=parsed,
                                       subject=el.subject, from_email=el.from_email,
                                       email_body=el.body, alias=alias)
            tags = _ai_tags_for(el.category, el.action_required)
            if tags:
                add_zendesk_ticket_tags(zd_ticket_id, sorted(tags))
        self.stdout.write(self.style.SUCCESS(
            f"{'Would re-route' if dry else 'Re-routed'} {matched} orphan email(s)."))
