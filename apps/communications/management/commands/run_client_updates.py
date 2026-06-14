"""Autonomous client-update runner.

Sends any DUE client progress updates as public Zendesk replies — but ONLY when
SystemSettings.client_updates_autosend is ON. With the flag OFF (the default),
this is a safe no-op: updates remain scheduled for an agent to send manually.

Intended to run from a Railway scheduled job (recommend hourly). Do NOT run an
in-process scheduler inside gunicorn — two workers would mean two schedulers.
The command is idempotent and safe to run on any cadence.

    python manage.py run_client_updates
    python manage.py run_client_updates --dry-run   # report what's due, send nothing
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Send due client progress updates (only when autosend is enabled)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="List the updates that are due without drafting or sending anything.",
        )

    def handle(self, *args, **options):
        from apps.communications import client_updates as cu
        from apps.config.models import SystemSettings

        autosend = bool(getattr(SystemSettings.get_instance(), 'client_updates_autosend', False))

        if options['dry_run']:
            due = list(cu.due_updates())
            self.stdout.write(
                f"autosend={'ON' if autosend else 'OFF'} — {len(due)} update(s) due "
                f"as of {timezone.now():%Y-%m-%d %H:%M %Z}:")
            for u in due:
                self.stdout.write(
                    f"  claim #{u.claim_id} ({u.claim.alf_claim_id or 'no ALF id'}) "
                    f"· {u.label} · due {u.due_at:%Y-%m-%d %H:%M}")
            if not autosend:
                self.stdout.write(self.style.WARNING(
                    "autosend is OFF — a real run would send nothing."))
            return

        result = cu.run_due_updates()
        if not result['enabled']:
            self.stdout.write(self.style.WARNING(
                "autosend is OFF — nothing sent. Enable it in Settings to activate."))
            return
        self.stdout.write(self.style.SUCCESS(
            f"Done: considered {result['considered']}, sent {result['sent']}, "
            f"held for agent {result['held']}, skipped {result['skipped']}, "
            f"failed/will-retry {result['failed']}."))
