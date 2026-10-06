from django.apps import AppConfig


class ProjectsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'projects'

    def ready(self):
        # Registers the Zenodo endpoint startup check.
        from . import checks  # noqa: F401
        from . import signals

        signals.connect()
