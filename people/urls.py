from django.urls import path

from . import views

urlpatterns = [
    path("me/", views.my_profile, name="me"),
    path("me/edit/", views.profile_edit, name="edit"),
    path("<int:pk>/", views.person_detail, name="detail"),
]
