from django.urls import path

from . import views

app_name = "backups"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("history/", views.history, name="history"),
    path("manual/", views.manual_backup, name="manual"),
    path("activity/", views.activity, name="activity"),
    path(
        "restores/<uuid:restore_public_id>/",
        views.restore_status,
        name="restore_status",
    ),
    path("<uuid:public_id>/", views.detail, name="detail"),
    path(
        "<uuid:public_id>/preflight/",
        views.restore_preflight,
        name="restore_preflight",
    ),
    path(
        "<uuid:public_id>/restore/",
        views.restore_confirmation,
        name="restore",
    ),
    path(
        "<uuid:public_id>/restore-review/",
        views.restore_review,
        name="restore_review",
    ),
]
