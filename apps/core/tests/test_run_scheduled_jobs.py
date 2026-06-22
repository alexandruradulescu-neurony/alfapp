"""The cron dispatcher: master kill-switch, heartbeat, and fault isolation."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.config.models import ServiceStatus
import apps.core.management.commands.run_scheduled_jobs as rsj


def _set_scheduler(enabled):
    # The dev DB (tests run against it) may already hold a SCHEDULER row, so
    # reset it to a known baseline rather than creating a duplicate.
    ServiceStatus.objects.update_or_create(
        service='SCHEDULER',
        defaults={'is_enabled': enabled, 'status': 'disconnected', 'last_error': '', 'metadata': {}})


class RunScheduledJobsTests(TestCase):
    def test_master_switch_off_runs_nothing(self):
        _set_scheduler(False)
        called = []
        with patch.object(rsj, 'JOBS', [('demo', lambda: called.append(1) or {})]):
            call_command('run_scheduled_jobs', stdout=StringIO())
        self.assertEqual(called, [])
        self.assertEqual(ServiceStatus.objects.get(service='SCHEDULER').status, 'stopped')

    def test_runs_jobs_and_writes_heartbeat(self):
        _set_scheduler(True)
        with patch.object(rsj, 'JOBS', [('demo', lambda: {'sent': 2})]):
            call_command('run_scheduled_jobs', stdout=StringIO())
        s = ServiceStatus.objects.get(service='SCHEDULER')
        self.assertEqual(s.status, 'running')
        self.assertIsNotNone(s.last_checked)
        self.assertEqual(s.last_error, '')
        self.assertEqual(s.metadata['jobs']['demo'], {'sent': 2})

    def test_one_failing_job_does_not_stop_the_others(self):
        _set_scheduler(True)
        ran = []

        def boom():
            raise RuntimeError('nope')

        def ok():
            ran.append(1)
            return {'ok': True}

        with patch.object(rsj, 'JOBS', [('boom', boom), ('ok', ok)]):
            call_command('run_scheduled_jobs', stdout=StringIO(), stderr=StringIO())
        self.assertEqual(ran, [1])  # the second job still ran
        s = ServiceStatus.objects.get(service='SCHEDULER')
        self.assertEqual(s.status, 'error')
        self.assertIn('boom', s.last_error)

    def test_dry_run_executes_nothing(self):
        _set_scheduler(True)
        called = []
        with patch.object(rsj, 'JOBS', [('demo', lambda: called.append(1) or {})]):
            call_command('run_scheduled_jobs', '--dry-run', stdout=StringIO())
        self.assertEqual(called, [])

    def test_client_updates_job_delegates_to_runner(self):
        # the real registered job just calls run_due_updates
        with patch('apps.communications.client_updates.run_due_updates',
                   return_value={'enabled': False}) as run:
            self.assertEqual(rsj._job_client_updates(), {'enabled': False})
        run.assert_called_once()

    def test_email_sweep_is_registered_but_dormant_by_default(self):
        from apps.config.models import SystemSettings
        self.assertIn('email_sweep', [name for name, _ in rsj.JOBS])
        ss = SystemSettings.get_instance()
        ss.email_sweep_autorun = False
        ss.save()
        with patch('apps.communications.services.process_incoming_emails') as sweep:
            result = rsj._job_email_sweep()
        sweep.assert_not_called()                       # never touches the live inbox while off
        self.assertEqual(result, {'enabled': False})

    def test_email_sweep_runs_only_when_flag_on(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.email_sweep_autorun = True
        ss.save()
        with patch('apps.communications.services.process_incoming_emails',
                   return_value={'processed': 0}) as sweep:
            result = rsj._job_email_sweep()
        sweep.assert_called_once()
        self.assertEqual(result, {'processed': 0})

    def test_recover_orphans_job_dormant_when_flag_off(self):
        from apps.config.models import SystemSettings
        self.assertIn('recover_orphans', [name for name, _ in rsj.JOBS])
        ss = SystemSettings.get_instance()
        ss.recover_orphan_emails = False
        ss.save()
        with patch('apps.communications.services.recover_orphan_emails') as fn:
            result = rsj._job_recover_orphans()
        fn.assert_not_called()
        self.assertEqual(result, {'enabled': False})

    def test_recover_orphans_job_runs_when_flag_on(self):
        from apps.config.models import SystemSettings
        ss = SystemSettings.get_instance()
        ss.recover_orphan_emails = True
        ss.save()
        with patch('apps.communications.services.recover_orphan_emails',
                   return_value={'matched': 3, 'dry_run': False}) as fn:
            result = rsj._job_recover_orphans()
        fn.assert_called_once_with(dry_run=False)
        self.assertEqual(result, {'enabled': True, 'matched': 3, 'dry_run': False})
