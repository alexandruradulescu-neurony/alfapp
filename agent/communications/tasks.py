"""
Scheduled tasks for the communications app.
Uses django-apscheduler for task scheduling.
"""

import logging
from datetime import datetime

from apscheduler.schedulers.base import SchedulerAlreadyRunningError
from django_apscheduler.jobstores import DjangoJobStore, register_events, register_job
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


def process_incoming_emails_task():
    """
    APScheduler task wrapper for processing incoming emails.
    Calls the service function and logs results.
    """
    from apps.communications.services import process_incoming_emails
    
    logger.info(f"[{datetime.now()}] Starting scheduled email processing task...")
    
    try:
        stats = process_incoming_emails()
        logger.info(
            f"Email processing completed. "
            f"Processed: {stats.get('processed', 0)}, "
            f"Matched: {stats.get('matched', 0)}, "
            f"Skipped: {stats.get('skipped_no_claim', 0)}, "
            f"Errors: {stats.get('errors', 0)}"
        )
        return stats
    except Exception as e:
        logger.error(f"Error in scheduled email processing task: {e}")
        return {'error': str(e)}


def register_scheduler_jobs():
    """
    Register all scheduled jobs with the APScheduler.
    Call this function during app ready() or in a management command.
    """
    jobstore = DjangoJobStore()

    try:
        from django_apscheduler.scheduler import get_scheduler
        scheduler = get_scheduler()

        # Register the jobstore
        scheduler.add_jobstore(jobstore, 'default')

        # Register process_incoming_emails task
        # Runs every 3 minutes, max 1 instance at a time
        register_job(
            scheduler,
            'default',
            process_incoming_emails_task,
            trigger=IntervalTrigger(minutes=3),
            id='process_incoming_emails',
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=60,
        )
        logger.info("Registered process_incoming_emails job (every 3 minutes)")

        # Register event listeners
        register_events(scheduler)

        # Start scheduler if not already running
        if not scheduler.running:
            scheduler.start()
            logger.info("Scheduler started successfully.")
        else:
            logger.info("Scheduler was already running.")

        logger.info("All scheduler jobs registered successfully.")

    except SchedulerAlreadyRunningError:
        logger.info("Scheduler is already running.")
    except Exception as e:
        logger.error(f"Error registering scheduler jobs: {e}")
        raise
