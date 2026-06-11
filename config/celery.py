"""Celery application.

Optional in local development (tasks run eagerly when no broker is
configured); docker-compose provides Redis + worker services.
"""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

try:
    from celery import Celery

    app = Celery("nexapos")
    app.config_from_object("django.conf:settings", namespace="CELERY")
    app.autodiscover_tasks()
except ImportError:  # celery not installed in minimal dev environments
    app = None
