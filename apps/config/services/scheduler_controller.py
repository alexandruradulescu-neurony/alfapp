"""Master on/off switch for LORA's scheduled jobs.

Scheduling itself is owned by a Railway cron job running
`python manage.py run_scheduled_jobs` (see that command). There is no in-process
scheduler to "start"/"stop" — the only control surface is this enable flag,
stored on ServiceStatus('SCHEDULER'), which the cron dispatcher checks each run.
"""

import logging
from typing import Dict, Any

from apps.config.models import ServiceStatus

logger = logging.getLogger(__name__)


class SchedulerController:
    """Enable/disable the cron dispatcher via the ServiceStatus master switch."""

    def toggle_enabled(self, enable: bool) -> Dict[str, Any]:
        """Flip the master switch the run_scheduled_jobs cron honors."""
        try:
            status, _ = ServiceStatus.objects.get_or_create(
                service='SCHEDULER',
                defaults={'status': 'stopped', 'is_enabled': enable},
            )
            was_enabled = status.is_enabled
            status.is_enabled = enable
            status.save()
            action = 'enabled' if enable else 'disabled'
            return {
                'success': True,
                'status': 'enabled' if enable else 'disabled',
                'message': f'Scheduler {action}',
                'previously': 'enabled' if was_enabled else 'disabled',
            }
        except Exception as e:
            logger.error(f'Error toggling scheduler enabled state: {e}')
            return {'success': False, 'status': 'error', 'message': str(e)}
