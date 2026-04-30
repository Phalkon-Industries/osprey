from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from projects.models import Project

from .forms import ProfileForm
from .models import Institution, Profile


def person_detail(request, pk: int):
    """Public profile page. The list of /people/ is no longer exposed.

    A profile is reachable by direct link (e.g. from a project's contributor
    list, or from the user's own "Your profile" link in the header).
    """
    User = get_user_model()
    person = get_object_or_404(
        User.objects.select_related("profile", "profile__institution"),
        pk=pk,
    )
    Profile.objects.get_or_create(user=person)
    person.refresh_from_db()

    is_self = request.user.is_authenticated and request.user.pk == person.pk

    contributions = (
        person.contributions.select_related("project").order_by("project__title")
    )

    # On a person's own profile, surface their drafts. On someone else's
    # profile, only show projects the viewer is allowed to see.
    if is_self:
        own_projects = Project.objects.filter(
            Q(created_by=person) | Q(contributions__user=person)
        ).distinct().order_by("-updated_at")
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
        },
    )


@login_required
def my_profile(request):
    """Shortcut to the signed-in user's own profile page."""
    return redirect("people:detail", pk=request.user.pk)


@login_required
def profile_edit(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        form = ProfileForm(request.POST, request.FILES, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated.")
            return redirect("people:detail", pk=request.user.pk)
    else:
        form = ProfileForm(instance=profile)
    return render(request, "people/edit.html", {"form": form, "profile": profile})


def institutions_list(request):
    institutions = Institution.objects.all()
    return render(request, "people/institutions.html", {"institutions": institutions})
