from django.urls import path

from . import views

urlpatterns = [
    path("", views.institutions_list, name="list"),
]
