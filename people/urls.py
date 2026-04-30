from django.urls import path

from . import views

urlpatterns = [
    path("", views.people_list, name="list"),
    path("<int:pk>/", views.person_detail, name="detail"),
]
