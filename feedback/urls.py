from django.urls import path

from . import views

app_name = "feedback"

urlpatterns = [
    path("review/", views.review, name="review"),
    path("submit/", views.submit, name="submit"),
    path("mine/", views.mine, name="mine"),
    path("reply/<int:feedback_id>/", views.user_reply, name="user_reply"),
    path(
        "reopen/<int:feedback_id>/",
        views.reopen_request,
        name="reopen_request",
    ),
    path("compose/", views.compose, name="compose"),
]
