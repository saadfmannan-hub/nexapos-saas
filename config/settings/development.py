"""Development settings — SQLite, console email, relaxed security."""
from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Plain static storage in dev (no manifest hashing)
STORAGES["staticfiles"] = {  # noqa: F405
    "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"
}

INTERNAL_IPS = ["127.0.0.1"]
