from django.urls import path

from . import views

urlpatterns = [
    path("", views.project_list, name="list"),
    path("new/", views.project_new, name="new"),
    path("<slug:slug>/", views.project_detail, name="detail"),
    path("<slug:slug>/edit/", views.project_edit, name="edit"),
    path("<slug:slug>/zenodo/sync/", views.project_zenodo_sync, name="zenodo_sync"),
    path("<slug:slug>/zenodo/publish/", views.project_zenodo_publish, name="zenodo_publish"),
]
