"""Keep each project's search index current. See projects/search.py."""

from django.apps import apps
from django.db.models.signals import m2m_changed, post_delete, post_migrate, post_save

from . import search


def _project_saved(sender, instance, update_fields=None, **kwargs):
    if update_fields is None or search.PROJECT_FIELDS & set(update_fields):
        search.rebuild(instance.pk)


def _child_changed(sender, instance, **kwargs):
    search.rebuild(instance.project_id)


def _tags_changed(sender, instance, action, reverse, pk_set, **kwargs):
    if not action.startswith("post_"):
        return
    Project = apps.get_model("projects", "Project")
    if reverse:
        # A tag's projects changed; pk_set is None after clear().
        ids = pk_set or []
    else:
        ids = [instance.pk] if isinstance(instance, Project) else []
    for pk in ids:
        search.rebuild(pk)


def _tag_saved(sender, instance, created, **kwargs):
    if created:
        return
    for pk in instance.projects.values_list("pk", flat=True):
        search.rebuild(pk)


def _migrated(sender, **kwargs):
    if sender.name == "projects":
        search.rebuild_missing()


def connect():
    Project = apps.get_model("projects", "Project")
    post_save.connect(_project_saved, sender=Project, dispatch_uid="search-project")
    for label in ("projects.Contribution", "projects.TagAssignment", "wiki.WikiPage"):
        model = apps.get_model(label)
        post_save.connect(_child_changed, sender=model, dispatch_uid=f"search-save-{label}")
        post_delete.connect(_child_changed, sender=model, dispatch_uid=f"search-delete-{label}")
    m2m_changed.connect(_tags_changed, sender=Project.tags.through, dispatch_uid="search-tags")
    post_save.connect(_tag_saved, sender=apps.get_model("projects", "Tag"), dispatch_uid="search-tag")
    post_migrate.connect(_migrated, dispatch_uid="search-migrate")
