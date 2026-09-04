"""Contributor claiming: invites, acceptance, declines, removal disputes.

The rules (decided 2026-09-04): listing someone is a claim about a
person, so nothing links to a profile without their acceptance. Rows go
unclaimed -> invited -> verified/declined; a declined iD can't be
re-invited by the same project (anti-nag; staff can clear it in admin).
Verified rows on published projects can only be removed through a staff
dispute; on drafts, leaving is self-serve because there's no public
record yet. External invites (no OSPREY account) send one ephemeral
email and store nothing.

Notification emitters run through _notify so they can never break the
action.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Contribution

logger = logging.getLogger(__name__)


def _notify(name: str, *args) -> None:
    try:
        from notifications import events

        getattr(events, name)(*args)
    except Exception:
        logger.exception("claiming notification %s failed", name)


def orcid_for(user) -> str:
    """The verified ORCID iD of a signed-in user, or ""."""
    if not user or not user.is_authenticated:
        return ""
    account = user.socialaccount_set.filter(provider="orcid").first()
    return account.uid if account else ""


def user_for_orcid(orcid_id: str):
    """The active OSPREY user holding this ORCID iD, or None."""
    if not orcid_id:
        return None
    return (
        get_user_model()
        .objects.filter(
            socialaccount__provider="orcid",
            socialaccount__uid=orcid_id,
            is_active=True,
        )
        .first()
    )


def request_confirmation(contribution: Contribution, by_user):
    """Ask a listed person (who has an OSPREY account) to confirm their
    credit. This is the only thing that moves a row to "Confirmation
    requested"; listing alone stays quiet. Returns an error string or
    None.
    """
    if not contribution.orcid_id:
        return "Attach an ORCID iD first."
    if contribution.claim_status == Contribution.CLAIM_VERIFIED:
        return "Already accepted."
    if contribution.claim_status == Contribution.CLAIM_DECLINED:
        return (
            "They declined this listing; it can't be asked again. Contact "
            "staff if that was a mistake."
        )
    if contribution.claim_status == Contribution.CLAIM_INVITED:
        return "Confirmation already requested."
    if user_for_orcid(contribution.orcid_id) is None:
        return "No OSPREY account holds that ORCID iD yet."
    contribution.claim_status = Contribution.CLAIM_INVITED
    contribution.invited_at = timezone.now()
    contribution.save(update_fields=["claim_status", "invited_at"])
    _notify("contributor_listed", contribution, by_user)
    return None


def send_osprey_invite(contribution: Contribution, by_user, email: str):
    """Invite someone with no OSPREY account to the platform, by one
    ephemeral email. Deliberately does NOT touch claim_status: the
    confirmation line starts when they actually join (signup sweep) or
    at publish. Because the address is never stored, re-sending means
    re-entering it; the view rate-limits that. Returns an error string
    or None."""
    email = (email or "").strip()
    if not email:
        return "Enter an email address."
    if not contribution.orcid_id:
        return "Attach an ORCID iD first."
    if contribution.claim_status in (
        Contribution.CLAIM_VERIFIED,
        Contribution.CLAIM_DECLINED,
        Contribution.CLAIM_DISPUTED,
    ):
        return "This listing already has an answer."
    if user_for_orcid(contribution.orcid_id) is not None:
        return "They're already on OSPREY; no email invite needed."
    _send_external_invite(contribution, by_user, email)
    # The invite implies the confirmation request: the moment they sign
    # up with this iD, the row is waiting on their Contributor Credits
    # page (drafts included). Re-sending while they still have no
    # account is allowed, which is why "invited" doesn't block above.
    if contribution.claim_status != Contribution.CLAIM_INVITED:
        contribution.claim_status = Contribution.CLAIM_INVITED
        contribution.invited_at = timezone.now()
        contribution.save(update_fields=["claim_status", "invited_at"])
    return None


def _send_external_invite(contribution: Contribution, by_user, email: str):
    """One ephemeral email to someone not on OSPREY. No token needed:
    claiming is gated on the ORCID match, so the mail is purely
    informational, and the outbox row deletes itself after sending."""
    from notifications.emails import SUBJECT_PREFIX, absolute_url
    from notifications.models import QueuedEmail

    project = contribution.project
    owner_name = f"@{by_user.get_username()}" if by_user else "A project owner"
    lines = [
        f"{owner_name} listed you as a contributor "
        f"({contribution.role}) on the OSPREY project "
        f"“{project.title}”.",
        "",
        "OSPREY is a catalog for open research engineering projects. To "
        "review and accept the credit, sign in with your ORCID iD "
        f"({contribution.orcid_id}) and open Settings -> Contributor "
        "Credits:",
        "",
        absolute_url("/settings/claims/"),
        "",
        "--",
        "This is a one-time invitation. OSPREY hasn't stored your email "
        "address and won't contact you again unless the project owner "
        "re-enters it. If this email is unwanted, you can simply ignore "
        "it; nothing further will be sent.",
    ]
    QueuedEmail.objects.create(
        user=None,
        to_address=email,
        ephemeral=True,
        group="invite",
        subject=f"{SUBJECT_PREFIX}You've been credited on {project.title}"[:300],
        body_text="\n".join(lines),
    )


def sweep_on_publish(project) -> int:
    """At publish, invite every unclaimed ORCID row whose person is
    already on OSPREY. Returns how many invites went out."""
    count = 0
    rows = project.contributions.filter(
        claim_status=Contribution.CLAIM_UNCLAIMED, user__isnull=True
    ).exclude(orcid_id="")
    for row in rows:
        if request_confirmation(row, project.created_by) is None:
            count += 1
    return count


def sweep_on_signup(user) -> int:
    """When someone joins via ORCID, flip their unclaimed rows on public
    projects to invited so their claims page fills up."""
    orcid_id = orcid_for(user)
    if not orcid_id:
        return 0
    count = 0
    rows = Contribution.objects.filter(
        orcid_id=orcid_id,
        claim_status=Contribution.CLAIM_UNCLAIMED,
        user__isnull=True,
        project__visibility="public",
    ).select_related("project")
    for row in rows:
        row.claim_status = Contribution.CLAIM_INVITED
        row.invited_at = timezone.now()
        row.save(update_fields=["claim_status", "invited_at"])
        _notify("contributor_listed", row, row.project.created_by)
        count += 1
    return count


def pending_for(user):
    """Invited rows waiting on this user's answer."""
    orcid_id = orcid_for(user)
    if not orcid_id:
        return Contribution.objects.none()
    return (
        Contribution.objects.filter(
            orcid_id=orcid_id, claim_status=Contribution.CLAIM_INVITED
        )
        .select_related("project", "project__created_by")
        .order_by("-invited_at")
    )


def verified_for(user):
    """Rows this user has accepted."""
    return (
        Contribution.objects.filter(
            user=user, claim_status__in=["verified", "disputed"]
        )
        .select_related("project")
        .order_by("project__title")
    )


def accept(contribution: Contribution, user):
    """Accept a listing: links the profile, shows the checkmark, and
    arms any pre-granted editor flag. Returns an error string or None."""
    if contribution.claim_status != Contribution.CLAIM_INVITED:
        return "This listing isn't awaiting your answer."
    if orcid_for(user) != contribution.orcid_id:
        return "This listing names a different ORCID iD."
    contribution.user = user
    contribution.claim_status = Contribution.CLAIM_VERIFIED
    contribution.save(update_fields=["user", "claim_status"])
    _notify("contributor_claim_resolved", contribution, True)
    return None


def decline(contribution: Contribution, user, report_reason=None):
    """Decline a listing; optionally file an abuse report to the
    moderation queue. Returns an error string or None."""
    if contribution.claim_status != Contribution.CLAIM_INVITED:
        return "This listing isn't awaiting your answer."
    if orcid_for(user) != contribution.orcid_id:
        return "This listing names a different ORCID iD."
    contribution.claim_status = Contribution.CLAIM_DECLINED
    contribution.save(update_fields=["claim_status"])
    _notify("contributor_claim_resolved", contribution, False)
    if report_reason is not None:
        _file_report(
            contribution,
            user,
            "Contributor listing declined and reported as abusive. "
            + (report_reason or ""),
        )
    return None


def leave_draft(contribution: Contribution, user):
    """Self-serve exit from a draft: no public record exists yet, so no
    staff involvement is needed. Returns an error string or None."""
    if contribution.user_id != user.id:
        return "Not your listing."
    if contribution.project.is_public:
        return "This project is published; removal goes through staff."
    contribution.user = None
    contribution.claim_status = Contribution.CLAIM_UNCLAIMED
    contribution.save(update_fields=["user", "claim_status"])
    return None


def request_removal(contribution: Contribution, user, reason: str = ""):
    """Removal from a published project is a staff-mediated dispute: the
    public credit record doesn't change on one side's say-so. Returns an
    error string or None."""
    if contribution.user_id != user.id:
        return "Not your listing."
    if not contribution.project.is_public:
        return "This project is a draft; you can leave it directly."
    if contribution.claim_status == Contribution.CLAIM_DISPUTED:
        return "Removal is already requested; staff will take it from here."
    contribution.claim_status = Contribution.CLAIM_DISPUTED
    contribution.save(update_fields=["claim_status"])
    _file_report(
        contribution,
        user,
        "Verified contributor requests removal from the project. "
        + (reason or ""),
    )
    return None


def _file_report(contribution: Contribution, reporter, reason: str):
    """Put the case in the moderation queue, where staff tooling lives."""
    from django.contrib.contenttypes.models import ContentType

    from moderation.models import Report

    report = Report.objects.create(
        reporter=reporter,
        target_ct=ContentType.objects.get_for_model(Contribution),
        target_id=contribution.pk,
        target_repr=(
            f"Contributor row “{contribution.display_name}” on "
            f"{contribution.project.title}"
        )[:300],
        context_url=contribution.project.get_absolute_url(),
        category="other",
        reason=reason.strip()[:2000],
    )
    _notify("content_report_filed", report)
    return report
