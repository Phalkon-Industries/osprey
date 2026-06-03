from django.urls import path

from . import views

app_name = "moderation"

urlpatterns = [
    path("queue/", views.report_queue, name="queue"),
    path(
        "report/new/<str:app_label>/<str:model>/<int:target_id>/",
        views.report_new,
        name="report_new",
    ),
    path("report/<int:report_id>/", views.report_detail, name="report_detail"),
    path("project/<int:project_id>/hide/", views.project_hide, name="project_hide"),
    path(
        "project/<int:project_id>/unhide/",
        views.project_unhide,
        name="project_unhide",
    ),
    path("user/<int:user_id>/suspend/", views.user_suspend, name="user_suspend"),
    path(
        "user/<int:user_id>/reinstate/",
        views.user_reinstate,
        name="user_reinstate",
    ),
    path("user/<int:user_id>/purge/", views.user_purge, name="user_purge"),
]
