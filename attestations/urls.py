from django.urls import path

from . import views

app_name = "attestations"

urlpatterns = [
    path("<slug:slug>/use-reports/", views.index, name="index"),
    path("<slug:slug>/use-reports/new/", views.new, name="new"),
    path(
        "<slug:slug>/use-reports/<int:use_report_id>/moderate/",
        views.moderate,
        name="moderate",
    ),
]
