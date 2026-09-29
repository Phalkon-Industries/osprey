"""Permission lookups for templates that render project chrome.

The project hero appears on every tab page (overview, wiki, discussion,
versions...), but only the overview view used to pass it the "can edit"
and "can publish a new version" flags, so the buttons showed on one tab
only. The hero now asks these tags itself.
"""

from django import template

from projects import zenodo_jobs
from projects.models import ProjectDeposit

register = template.Library()


@register.simple_tag
def can_edit_project(project, user) -> bool:
    return bool(project and project.editable_by(user))


@register.simple_tag
def can_publish_new_version(project, user) -> bool:
    if not project or not project.publishable_by(user) or not project.accepts_publish:
        return False
    deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
    if deposit is None or deposit.state != ProjectDeposit.STATE_PUBLISHED:
        return False
    return zenodo_jobs.pending_job(project) is None
