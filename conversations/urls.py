from django.urls import path

from . import views

app_name = "conversations"

urlpatterns = [
    path("<slug:slug>/discussion/", views.thread_index, name="index"),
    path("<slug:slug>/discussion/new/", views.thread_new, name="new"),
    path("<slug:slug>/discussion/<int:thread_id>/", views.thread_detail, name="detail"),
    path(
        "<slug:slug>/discussion/<int:thread_id>/follow/",
        views.toggle_follow,
        name="toggle_follow",
    ),
    path(
        "<slug:slug>/discussion/<int:thread_id>/answer/",
        views.mark_answer,
        name="mark_answer",
    ),
    path(
        "<slug:slug>/discussion/<int:thread_id>/accept/",
        views.accept_answer,
        name="accept_answer",
    ),
    path(
        "<slug:slug>/discussion/<int:thread_id>/close/",
        views.close_thread,
        name="close",
    ),
]
