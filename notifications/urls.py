from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("menu/", views.menu, name="menu"),
    path("read-all/", views.mark_all_read, name="mark_all_read"),
    path("<int:pk>/", views.open_notification, name="open"),
]
