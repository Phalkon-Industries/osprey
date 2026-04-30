from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404, render

from .models import Institution


def people_list(request):
    User = get_user_model()
    users = User.objects.select_related("profile", "profile__institution").order_by(
        "username"
    )
    return render(request, "people/list.html", {"users": users})


def person_detail(request, pk: int):
    User = get_user_model()
    person = get_object_or_404(
        User.objects.select_related("profile", "profile__institution"),
        pk=pk,
    )
    contributions = (
        person.contributions.select_related("project").order_by("project__title")
    )
    return render(
        request,
        "people/detail.html",
        {"person": person, "contributions": contributions},
    )


def institutions_list(request):
    institutions = Institution.objects.all()
    return render(request, "people/institutions.html", {"institutions": institutions})
