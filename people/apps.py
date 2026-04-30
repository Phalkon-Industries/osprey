from django.apps import AppConfig


class PeopleConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'people'
    def ready(self) -> None:  # noqa: D401
        from . import signals  # noqa: F401