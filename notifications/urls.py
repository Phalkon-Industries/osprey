from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("menu/", views.menu, name="menu"),
    path("settings/", views.notification_settings, name="settings"),
    path("unsubscribe/<str:token>/", views.unsubscribe, name="unsubscribe"),
    path("banner-dismiss/", views.dismiss_email_banner, name="dismiss_email_banner"),
    path("read-all/", views.mark_all_read, name="mark_all_read"),
    path("<int:pk>/", views.open_notification, name="open"),
]
