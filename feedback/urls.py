from django.urls import path

from . import views

app_name = "feedback"

urlpatterns = [
    path("review/", views.review, name="review"),
    path("submit/", views.submit, name="submit"),
]
