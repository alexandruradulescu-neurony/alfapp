from django.apps import AppConfig


class CommunicationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.communications'
    label = 'communications'

    def ready(self):
        """
        Initialize scheduled tasks when the app is ready.
        Uncomment the following line to auto-register scheduler jobs:
        Note: In production, consider using a separate management command
        to start the scheduler to avoid multiple instances.
        """
        # from apps.communications.tasks import register_scheduler_jobs
        # register_scheduler_jobs()
        pass
