from django.urls import path

from . import views

urlpatterns = [
    path("", views.project_list, name="list"),
    path("new/", views.project_new, name="new"),
    path("<slug:slug>/", views.project_detail, name="detail"),
    path("<slug:slug>/edit/", views.project_edit, name="edit"),
    path("<slug:slug>/versions/", views.project_versions, name="versions"),
    path(
        "<slug:slug>/zenodo/new-version/",
        views.project_zenodo_new_version,
        name="zenodo_new_version",
    ),
]
