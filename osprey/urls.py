from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import RedirectView

from api.urls import api
from core import views as core_views
from feedback import views as feedback_views
from projects import views as project_views

admin.site.site_header = "OSPREY admin"
admin.site.site_title = "OSPREY admin"
admin.site.index_title = "Site administration"

urlpatterns = [
    path("", core_views.home, name="home"),
    path("about/", core_views.about, name="about"),
    path("about/licenses/", core_views.license_guide, name="license_guide"),
    path("about/markdown/", core_views.markdown_guide, name="markdown_guide"),
    path("about/derivatives/", core_views.derivative_guide, name="derivative_guide"),
    path("about/privacy/", core_views.privacy, name="privacy"),
    path(
        "about/privacy/request/",
        feedback_views.privacy_request,
        name="privacy_request",
    ),
    path("settings/", core_views.settings_home, name="settings"),
    path("admin/", admin.site.urls),
    path("", include(("core.urls", "core"), namespace="core")),
    path("accounts/", include("allauth.urls")),
    path("login/", core_views.login, name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("projects/", include(("projects.urls", "projects"), namespace="projects")),
    path("projects/", include(("wiki.urls", "wiki"), namespace="wiki")),
    path(
        "projects/",
        include(("use_reports.urls", "use_reports"), namespace="use_reports"),
    ),
    path(
        "projects/",
        include(("conversations.urls", "conversations"), namespace="conversations"),
    ),
    path(
        "p/<uuid:public_id>/",
        project_views.project_permalink,
        name="project_permalink",
    ),
    path("people/", include(("people.urls", "people"), namespace="people")),
    path(
        "institutions/",
        include(("people.urls_institutions", "institutions"), namespace="institutions"),
    ),
    path("staff-messages/", include("feedback.urls")),
    # The app lived at /feedback/ before the Staff Messages rename; old
    # notification rows still link there.
    path(
        "feedback/mine/",
        RedirectView.as_view(pattern_name="feedback:mine"),
    ),
    path(
        "feedback/review/",
        RedirectView.as_view(pattern_name="feedback:review"),
    ),
    path(
        "moderation/",
        include(("moderation.urls", "moderation"), namespace="moderation"),
    ),
    path(
        "inbox/",
        include(("notifications.urls", "notifications"), namespace="notifications"),
    ),
    path("api/v1/", api.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
