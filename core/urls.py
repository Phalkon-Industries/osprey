from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("staff/", views.staff_dashboard, name="staff_dashboard"),
]
