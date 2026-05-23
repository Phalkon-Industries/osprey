from django.contrib.auth import get_user_model
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from projects.models import Project

from .forms import ProfileForm
from .models import Profile


def _verified_orcid_for(user) -> str:
    account = user.socialaccount_set.filter(provider="orcid").first()
    if account is None:
        return ""
    identifier = (account.extra_data or {}).get("orcid-identifier") or {}
    account_orcid = (identifier.get("path") or "").strip()
    if account_orcid:
        return account_orcid
    return getattr(user.profile, "orcid_placeholder", "")


def person_detail(request, pk: int):
    """Public profile page. The list of /people/ is no longer exposed.

    A profile is reachable by direct link (e.g. from a project's contributor
    list, or from the user's own "Your profile" link in the header).
    """
    User = get_user_model()
    person = get_object_or_404(
        User.objects.select_related("profile"),
        pk=pk,
    )
    Profile.objects.get_or_create(user=person)
    person.refresh_from_db()

    is_self = request.user.is_authenticated and request.user.pk == person.pk
    verified_orcid = _verified_orcid_for(person)

    contributions = person.contributions.select_related("project").order_by(
        "project__title"
    )

    # On a person's own profile, surface their drafts. On someone else's
    # profile, only show projects the viewer is allowed to see.
    if is_self:
        own_projects = (
            Project.objects.filter(Q(created_by=person) | Q(contributions__user=person))
            .distinct()
            .order_by("-updated_at")
        )
    else:
        own_projects = None

    return render(
        request,
        "people/detail.html",
        {
            "person": person,
            "contributions": contributions,
            "is_self": is_self,
            "own_projects": own_projects,
            "verified_orcid": verified_orcid,
        },
    )


@login_required
def my_profile(request):
    """Shortcut to the signed-in user's own profile page."""
    return redirect("people:detail", pk=request.user.pk)


@login_required
def profile_edit(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if not profile.usertag_locked:
        return redirect("people:onboarding")
    verified_orcid = _verified_orcid_for(request.user)
    if request.method == "POST":
        form = ProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
            return redirect("people:detail", pk=request.user.pk)
    else:
        form = ProfileForm(instance=profile)
    return render(
        request,
        "people/edit.html",
        {"form": form, "profile": profile, "verified_orcid": verified_orcid},
    )


@login_required
def profile_onboarding(request):
    """First-login setup. The only place the user tag is editable."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if profile.usertag_locked:
        return redirect("people:edit")
    verified_orcid = _verified_orcid_for(request.user)
    if request.method == "POST":
        form = ProfileForm(
            request.POST, request.FILES, instance=profile, allow_usertag=True
        )
        if form.is_valid():
            form.save()
            messages.success(request, "Welcome to OSPREY. Your profile is set.")
            return redirect("people:detail", pk=request.user.pk)
    else:
        form = ProfileForm(instance=profile, allow_usertag=True)
    return render(
        request,
        "people/onboarding.html",
        {"form": form, "profile": profile, "verified_orcid": verified_orcid},
    )


@login_required
@require_POST
def account_deactivate(request):
    user = request.user
    user.is_active = False
    user.save(update_fields=["is_active"])
    logout(request)
    messages.info(
        request,
        "Your account has been deactivated. The public profile remains, and ORCID sign-in can reactivate it.",
    )
    return redirect("home")


def institution_detail(request, slug: str):
    """List visible projects whose institution list contains a matching name.

    Institutions are stored as free text. A project may list multiple
    affiliations one per line or comma-separated. This view matches any
    project whose institution list contains a name whose slug equals
    `slug`, and surfaces the most-used spelling for the heading.
    """
    from projects.views import _visible_projects_for

    qs = _visible_projects_for(request.user).exclude(institution="")
    matches: list = []
    counts: dict[str, int] = {}
    for p in qs:
        for name in p.institutions:
            if slugify(name) == slug:
                matches.append(p)
                counts[name] = counts.get(name, 0) + 1
                break
    if not matches:
        raise Http404
    display_name = max(counts.items(), key=lambda kv: kv[1])[0]
    return render(
        request,
        "people/institution_detail.html",
        {
            "institution_name": display_name,
            "projects": sorted(matches, key=lambda p: p.updated_at, reverse=True),
        },
    )
