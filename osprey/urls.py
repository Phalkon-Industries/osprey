from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from api.urls import api
from core import views as core_views

admin.site.site_header = "OSPREY database admin"
admin.site.site_title = "OSPREY admin"
admin.site.index_title = "Data tables and maintenance"

urlpatterns = [
    path("", core_views.home, name="home"),
    path("about/", core_views.about, name="about"),
    path("about/licenses/", core_views.license_guide, name="license_guide"),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("login/", auth_views.LoginView.as_view(template_name="auth/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("projects/", include(("projects.urls", "projects"), namespace="projects")),
    path("people/", include(("people.urls", "people"), namespace="people")),
    path(
        "institutions/",
        include(("people.urls_institutions", "institutions"), namespace="institutions"),
    ),
    path("feedback/", include("feedback.urls")),
    path("api/v1/", api.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
