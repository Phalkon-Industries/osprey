"""Lineage claims: declare, activate at publish, respond, withdraw.

The rules live in planning/features/lineage.md ("Converged design",
2026-09-03). Short version: edges are declared from the child side and
pinned to versions on both ends; claims are live immediately, and the
parent's team can dispute one at any time (and retract the dispute);
edges declared on a draft stay dormant until publish; nothing is ever
edited or deleted, only withdrawn.

Notification emitters are called through _notify so a notification
failure can never break a claim.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from .models import LineageEdge, Project, ProjectDepositVersion

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"/projects/([^/?#]+)")
_PERMALINK_RE = re.compile(
    r"/p/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

RELATION_PHRASES = {
    "derived_from": "is derived from",
    "uses": "uses",
}


def relation_phrase(relation: str) -> str:
    return RELATION_PHRASES.get(relation, relation)


def latest_version(project: Project) -> ProjectDepositVersion | None:
    """The most recent published version row for a project, if any."""
    return (
        ProjectDepositVersion.objects.filter(deposit__project=project)
        .order_by("-version_index")
        .first()
    )


def resolve_target(value: str) -> Project | None:
    """Turn what the user pasted (slug, project URL, or permalink) into a
    Project, or None."""
    value = (value or "").strip()
    if not value:
        return None
    match = _PERMALINK_RE.search(value)
    if match:
        return Project.objects.filter(public_id=match.group(1)).first()
    match = _SLUG_RE.search(value)
    slug = match.group(1) if match else value
    if "/" in slug or " " in slug:
        return None
    return Project.objects.filter(slug=slug).first()


def _notify(name: str, *args) -> None:
    try:
        from notifications import events

        getattr(events, name)(*args)
    except Exception:
        logger.exception("lineage notification %s failed", name)


def _go_live(edge: LineageEdge) -> None:
    """Activate a claim: stamp claimed_at and tell the parent's team.

    Dispute-only model: the claim is live immediately, no approval step.
    The notification is informational; the parent can dispute anytime.
    """
    edge.claimed_at = timezone.now()
    edge.save()
    _notify("lineage_claimed", edge)


def declare(
    child: Project,
    target_value: str,
    relation: str,
    user,
    parent_version_id=None,
    defer=False,
):
    """Create a claim from `child` toward whatever `target_value` names.

    `parent_version_id` pins the claim to a specific published version of
    the parent; invalid or missing ids fall back to the latest version.
    `defer=True` keeps the claim dormant even on a public child: used by
    the new-version flow, where the claim belongs to the version being
    built and must only go live (pinned to it) when that version
    publishes. Returns (edge, error): exactly one is None.
    """
    if relation not in RELATION_PHRASES:
        return None, "Pick a relation."
    parent = resolve_target(target_value)
    if parent is None:
        return None, f"No OSPREY project found for “{target_value}”."
    if parent.pk == child.pk:
        return None, "A project can't be related to itself."
    if not parent.is_public:
        return None, f"“{parent.title}” isn't published, so it can't be linked yet."
    parent_version = latest_version(parent)
    if parent_version_id:
        pinned = ProjectDepositVersion.objects.filter(
            pk=parent_version_id, deposit__project=parent
        ).first()
        if pinned is not None:
            parent_version = pinned
    goes_live = child.is_public and not defer
    child_version = latest_version(child) if goes_live else None
    duplicate = (
        LineageEdge.objects.filter(
            parent=parent,
            child=child,
            relation=relation,
            parent_version=parent_version,
            child_version=child_version,
        )
        .exclude(status=LineageEdge.STATUS_WITHDRAWN)
        .exists()
    )
    if duplicate:
        return None, (
            f"This project already {relation_phrase(relation)} "
            f"“{parent.title}” at that version."
        )
    edge = LineageEdge.objects.create(
        parent=parent,
        child=child,
        relation=relation,
        parent_version=parent_version,
        child_version=child_version,
        declared_by=user if user and user.is_authenticated else None,
    )
    if goes_live:
        _go_live(edge)
    return edge, None


def activate_pending(child: Project) -> int:
    """Called after a successful publish: pin dormant edges to the new
    version and notify the parents. Returns how many went live."""
    version = latest_version(child)
    dormant = LineageEdge.objects.filter(
        child=child,
        claimed_at__isnull=True,
        status=LineageEdge.STATUS_ACTIVE,
    )
    count = 0
    for edge in dormant:
        edge.child_version = version
        _go_live(edge)
        count += 1
    return count


def respond(edge: LineageEdge, user, action: str, reason: str = ""):
    """Parent-side dispute, or retraction of one. Returns an error
    string or None."""
    if not edge.parent.editable_by(user):
        return "Only the linked project's team can respond to this claim."
    if edge.is_withdrawn:
        return "This link was withdrawn; there's nothing to respond to."
    if edge.claimed_at is None:
        return "This claim isn't live yet."
    if action == "dispute":
        edge.status = LineageEdge.STATUS_DISPUTED
        edge.dispute_reason = (reason or "").strip()[:500]
    elif action == "retract":
        if edge.status != LineageEdge.STATUS_DISPUTED:
            return "This link isn't disputed."
        edge.status = LineageEdge.STATUS_ACTIVE
        edge.dispute_reason = ""
    else:
        return "Unknown action."
    edge.responded_at = timezone.now()
    edge.save()
    _notify("lineage_responded", edge, user)
    return None


def withdraw(edge: LineageEdge, user):
    """Child-side withdrawal. The row stays forever, labeled. Returns an
    error string or None."""
    if not edge.child.editable_by(user):
        return "Only the claiming project's team can withdraw this link."
    if edge.is_withdrawn:
        return "Already withdrawn."
    edge.status = LineageEdge.STATUS_WITHDRAWN
    edge.responded_at = timezone.now()
    edge.save()
    if edge.claimed_at is not None:
        _notify("lineage_withdrawn", edge, user)
    return None
