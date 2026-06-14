from django.apps import AppConfig


class ConfigConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.config'
    label = 'config'

    def ready(self):
        from . import checks  # noqa: F401  (registers deploy-time system checks)
