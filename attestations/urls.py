from django.urls import path

from . import views

app_name = "attestations"

urlpatterns = [
    path("<slug:slug>/attestations/", views.index, name="index"),
    path("<slug:slug>/attestations/new/", views.new, name="new"),
    path(
        "<slug:slug>/attestations/<int:attestation_id>/moderate/",
        views.moderate,
        name="moderate",
    ),
]
