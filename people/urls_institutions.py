from django.urls import path

from . import views

urlpatterns = [
    path("<slug:slug>/", views.institution_detail, name="detail"),
]
