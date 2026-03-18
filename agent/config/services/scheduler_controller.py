import logging
from typing import Dict, Any
from django.utils import timezone
from apps.config.models import ServiceStatus

logger = logging.getLogger(__name__)


class SchedulerController:
    """Control the email processing scheduler (start/stop)."""
    
    def start(self) -> Dict[str, Any]:
        """Start the email processing scheduler."""
        try:
            from apps.communications.tasks import get_scheduler
            
            scheduler = get_scheduler()
            
            if scheduler is None:
                return {
                    'success': False,
                    'status': 'error',
                    'message': 'Scheduler not initialized'
                }
            
            if scheduler.running:
                return {
                    'success': False,
                    'status': 'running',
                    'message': 'Scheduler is already running'
                }
            
            scheduler.start()
            
            # Update status
            status, _ = ServiceStatus.objects.get_or_create(
                service='SCHEDULER',
                defaults={'status': 'running', 'is_enabled': True}
            )
            status.status = 'running'
            status.last_checked = timezone.now()
            status.save()
            
            logger.info('Email scheduler started')
            
            return {
                'success': True,
                'status': 'running',
                'message': 'Email scheduler started successfully'
            }
            
        except Exception as e:
            logger.error(f'Error starting scheduler: {e}')
            return {
                'success': False,
                'status': 'error',
                'message': str(e)
            }
    
    def stop(self) -> Dict[str, Any]:
        """Stop the email processing scheduler."""
        try:
            from apps.communications.tasks import get_scheduler
            
            scheduler = get_scheduler()
            
            if scheduler is None:
                return {
                    'success': False,
                    'status': 'error',
                    'message': 'Scheduler not initialized'
                }
            
            if not scheduler.running:
                return {
                    'success': False,
                    'status': 'stopped',
                    'message': 'Scheduler is not running'
                }
            
            scheduler.shutdown(wait=False)
            
            # Update status
            status, _ = ServiceStatus.objects.get_or_create(
                service='SCHEDULER',
                defaults={'status': 'stopped', 'is_enabled': True}
            )
            status.status = 'stopped'
            status.last_checked = timezone.now()
            status.save()
            
            logger.info('Email scheduler stopped')
            
            return {
                'success': True,
                'status': 'stopped',
                'message': 'Email scheduler stopped successfully'
            }
            
        except Exception as e:
            logger.error(f'Error stopping scheduler: {e}')
            return {
                'success': False,
                'status': 'error',
                'message': str(e)
            }
    
    def toggle_enabled(self, enable: bool) -> Dict[str, Any]:
        """Enable or disable the scheduler."""
        try:
            status, created = ServiceStatus.objects.get_or_create(
                service='SCHEDULER',
                defaults={'status': 'stopped', 'is_enabled': enable}
            )
            
            was_enabled = status.is_enabled
            status.is_enabled = enable
            status.save()
            
            action = 'enabled' if enable else 'disabled'
            
            return {
                'success': True,
                'status': 'enabled' if enable else 'disabled',
                'message': f'Scheduler {action}',
                'previously': 'enabled' if was_enabled else 'disabled'
            }
            
        except Exception as e:
            logger.error(f'Error toggling scheduler enabled state: {e}')
            return {
                'success': False,
                'status': 'error',
                'message': str(e)
            }
    
    def get_info(self) -> Dict[str, Any]:
        """Get scheduler information and status."""
        try:
            from apps.communications.tasks import get_scheduler
            
            scheduler = get_scheduler()
            status_obj = ServiceStatus.objects.filter(service='SCHEDULER').first()
            
            if scheduler is None:
                return {
                    'success': False,
                    'running': False,
                    'message': 'Scheduler not initialized'
                }
            
            jobs = []
            for job in scheduler.get_jobs():
                jobs.append({
                    'id': job.id,
                    'next_run': str(job.next_run_time) if job.next_run_time else None
                })
            
            return {
                'success': True,
                'running': scheduler.running,
                'enabled': status_obj.is_enabled if status_obj else True,
                'jobs': jobs,
                'status': status_obj.status if status_obj else 'unknown'
            }
            
        except Exception as e:
            logger.error(f'Error getting scheduler info: {e}')
            return {
                'success': False,
                'running': False,
                'message': str(e)
            }
