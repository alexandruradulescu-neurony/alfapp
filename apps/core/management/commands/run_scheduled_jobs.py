"""The single entry point for ALL of LORA's scheduled background work.

Run this from one Railway scheduled (cron) job — e.g. every 10 minutes:

    python manage.py run_scheduled_jobs

It dispatches each registered job in turn (a failure in one never stops the
others), then records a heartbeat on ServiceStatus('SCHEDULER') so the Settings
page can show that the cron is alive and when it last ran. To add a future job,
append one entry to JOBS below — nothing else changes.

Two independent switches guard it:
  - the MASTER kill-switch: ServiceStatus('SCHEDULER').is_enabled (toggled on the
    Settings page). Off → this command is a no-op.
  - each job's OWN flag (e.g. client updates only send when
    SystemSettings.client_updates_autosend is on). The dispatcher just calls the
    job; the job decides whether it actually does anything.

Do NOT run an in-process scheduler inside gunicorn — two workers would mean two
schedulers. This command + a Railway cron is the supported topology.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

SCHEDULER_SERVICE = 'SCHEDULER'


def _job_client_updates():
    """Send any due client progress updates (no-op unless autosend is on)."""
    from apps.communications.client_updates import run_due_updates
    return run_due_updates()


def _job_sync_aliases():
    """Pull each claim's submission alias from its Zendesk ticket into LORA so the
    sweep can match inbound mail LOCALLY (Zendesk search is unreliable for email
    fields — see project_email_system). only_missing keeps it cheap: after the first
    full sync, each tick fetches only claims that still lack an alias (i.e. new ones).
    Shares the sweep's gate — no point syncing if the sweep isn't matching. Runs
    BEFORE the sweep in JOBS so a new claim's alias is present when its first reply
    arrives."""
    from apps.config.models import SystemSettings
    if not getattr(SystemSettings.get_instance(), 'email_sweep_autorun', False):
        return {'enabled': False}
    from apps.integrations.services import sync_claim_aliases
    return {'enabled': True, **sync_claim_aliases(only_missing=True)}


def _job_email_sweep():
    """Sweep the shared inbox for institution replies. DORMANT by design: runs
    only when SystemSettings.email_sweep_autorun is on (default off) — until then
    email stays button-driven per ticket. See project_email_system."""
    from apps.config.models import SystemSettings
    if not getattr(SystemSettings.get_instance(), 'email_sweep_autorun', False):
        return {'enabled': False}
    from apps.communications.services import process_incoming_emails
    return process_incoming_emails()


def _job_recover_orphans():
    """Re-route orphaned EmailLogs to their tickets. Idempotent: already-routed
    emails are no longer orphans and are skipped. Turn ON to clear the backlog,
    watch one run, then turn OFF."""
    from apps.config.models import SystemSettings
    if not getattr(SystemSettings.get_instance(), 'recover_orphan_emails', False):
        return {'enabled': False}
    from apps.communications.services import recover_orphan_emails
    return {'enabled': True, **recover_orphan_emails(dry_run=False)}


# (name, callable) — the callable runs the job and returns a small summary dict.
# Add a future periodic job by appending one line. Each job decides for itself
# whether it actually does anything (its own flag), so registering one here is
# safe; the email sweep is registered but gated OFF until explicitly enabled.
JOBS = [
    ('client_updates', _job_client_updates),
    ('sync_aliases', _job_sync_aliases),
    ('email_sweep', _job_email_sweep),
    ('recover_orphans', _job_recover_orphans),
]


class Command(BaseCommand):
    help = "Run all due scheduled jobs (gated by the master Scheduler switch)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help="List the registered jobs without running any.")
        parser.add_argument('--job', metavar='NAME',
                            help="Run only the named job (still respects the master switch).")

    def handle(self, *args, **options):
        from apps.config.models import ServiceStatus

        jobs = JOBS
        if options.get('job'):
            jobs = [j for j in JOBS if j[0] == options['job']]
            if not jobs:
                self.stderr.write(self.style.ERROR(
                    f"Unknown job '{options['job']}'. Known: {', '.join(n for n, _ in JOBS)}"))
                return

        if options['dry_run']:
            self.stdout.write(f"{len(jobs)} registered job(s): "
                              f"{', '.join(n for n, _ in jobs)} (dry-run, nothing executed).")
            return

        status, _ = ServiceStatus.objects.get_or_create(
            service=SCHEDULER_SERVICE,
            defaults={'status': ServiceStatus.STATUS_STOPPED, 'is_enabled': True})
        if not status.is_enabled:
            self.stdout.write(self.style.WARNING(
                "Scheduler is disabled (master switch off) — nothing run."))
            status.status = ServiceStatus.STATUS_STOPPED
            status.last_checked = timezone.now()
            status.save(update_fields=['status', 'last_checked'])
            return

        results, errors = {}, []
        for name, fn in jobs:
            try:
                results[name] = fn() or {}
                self.stdout.write(f"  {name}: {results[name]}")
            except Exception as e:  # one job failing must not stop the rest
                errors.append(f"{name}: {e}")
                results[name] = {'error': str(e)}
                self.stderr.write(self.style.ERROR(f"  {name} FAILED: {e}"))

        status.status = ServiceStatus.STATUS_ERROR if errors else ServiceStatus.STATUS_RUNNING
        status.last_checked = timezone.now()
        status.last_error = ' | '.join(errors)
        status.metadata = {'ran_at': timezone.now().isoformat(), 'jobs': results}
        status.save(update_fields=['status', 'last_checked', 'last_error', 'metadata'])

        msg = f"Ran {len(jobs)} job(s); {len(errors)} failed."
        self.stdout.write((self.style.ERROR if errors else self.style.SUCCESS)(msg))
