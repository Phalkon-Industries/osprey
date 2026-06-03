from __future__ import annotations

from django.contrib.contenttypes.models import ContentType

from .models import ModerationLog


def log_action(actor, action: str, target=None, reason: str = "") -> ModerationLog:
    """Append a staff moderation action to the audit log."""
    kwargs = {
        "actor": actor if getattr(actor, "is_authenticated", False) else None,
        "action": action,
        "reason": reason or "",
    }
    if target is not None:
        kwargs["target_ct"] = ContentType.objects.get_for_model(type(target))
        kwargs["target_id"] = getattr(target, "pk", None)
        kwargs["target_repr"] = str(target)[:300]
    return ModerationLog.objects.create(**kwargs)
