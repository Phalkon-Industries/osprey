from django.urls import path

from . import views

app_name = "wiki"

urlpatterns = [
    path("<slug:slug>/wiki/", views.index, name="index"),
    path("<slug:slug>/wiki/new/", views.edit, name="new"),
    path("<slug:slug>/wiki/review/", views.review, name="review"),
    path(
        "<slug:slug>/wiki/review/<int:revision_id>/",
        views.review_action,
        name="review_action",
    ),
    path("<slug:slug>/wiki/<slug:page_slug>/", views.detail, name="detail"),
    path("<slug:slug>/wiki/<slug:page_slug>/edit/", views.edit, name="edit"),
    path("<slug:slug>/wiki/<slug:page_slug>/history/", views.history, name="history"),
]
