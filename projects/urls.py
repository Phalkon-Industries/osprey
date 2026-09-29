from django.urls import path

from . import views, views_indexing

urlpatterns = [
    # Before the <slug> routes, or "register" is taken for a project slug.
    path("register/", views.project_register, name="register"),
    path("index/", views_indexing.index_submit, name="index_submit"),
    path("", views.project_list, name="list"),
    path("orcid-search/", views.orcid_search_json, name="orcid_search"),
    path("lineage-lookup/", views.lineage_lookup, name="lineage_lookup"),
    path("new/", views.project_new, name="new"),
    path("<slug:slug>/", views.project_detail, name="detail"),
    path("<slug:slug>/edit/", views.project_edit, name="edit"),
    path("<slug:slug>/delete/", views.project_delete, name="delete"),
    path("<slug:slug>/watch/", views.watch_toggle, name="watch_toggle"),
    path("<slug:slug>/versions/", views.project_versions, name="versions"),
    path("<slug:slug>/files/", views.project_files, name="files"),
    path("<slug:slug>/citations/", views.project_citations, name="citations"),
    path("<slug:slug>/lineage/", views.project_lineage, name="lineage"),
    path(
        "<slug:slug>/lineage/respond/<int:edge_id>/",
        views.lineage_respond,
        name="lineage_respond",
    ),
    path(
        "<slug:slug>/lineage/withdraw/<int:edge_id>/",
        views.lineage_withdraw,
        name="lineage_withdraw",
    ),
    path(
        "<slug:slug>/transfer/",
        views.ownership_transfer_respond,
        name="ownership_transfer_respond",
    ),
    path(
        "<slug:slug>/zenodo/new-version/",
        views.project_zenodo_new_version,
        name="zenodo_new_version",
    ),
    path(
        "<slug:slug>/zenodo/refresh/",
        views.zenodo_refresh,
        name="zenodo_refresh",
    ),
    path(
        "<slug:slug>/zenodo/retry/",
        views.zenodo_job_retry,
        name="zenodo_retry",
    ),
]
