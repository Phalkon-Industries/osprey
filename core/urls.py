from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("staff/", views.staff_dashboard, name="staff_dashboard"),
    path("staff/signup-gate/", views.signup_gate, name="signup_gate"),
]
