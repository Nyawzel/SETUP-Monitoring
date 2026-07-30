from django.apps import AppConfig


class MonitoringConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'monitoring'

    def ready(self):
        # Importing this here (not at module load time) is required —
        # Django signals only get connected once this module is actually
        # imported, and ready() is the documented place to do that safely.
        from . import signals  # noqa: F401