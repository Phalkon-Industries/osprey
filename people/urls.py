from django.urls import path

from . import views

urlpatterns = [
    path("me/", views.my_profile, name="me"),
    path("me/welcome/", views.profile_onboarding, name="onboarding"),
    path("me/edit/", views.profile_edit, name="edit"),
    path("me/deactivate/", views.account_deactivate, name="deactivate"),
    path("<int:pk>/", views.person_detail, name="detail"),
    path("<int:pk>/follow/", views.follow_toggle, name="follow_toggle"),
]
